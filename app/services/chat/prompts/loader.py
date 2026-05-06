"""System prompt loader.

Each agent has a markdown file in this folder. The loader reads the
file and optionally appends a "Sobre el comensal" block built from the
authenticated user's taste profile so the bot can greet by name and
respect declared allergies. When the user has a populated wishlist
(WantToTryDish rows), the block also lists the top 7 oldest entries so
the agent can pull recall threads — "¿te animaste con el risotto que
guardaste hace dos meses?" — without an extra tool call.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatAgent, TastePillar, UserTasteProfile
from app.models.dish import Dish, WantToTryDish
from app.models.restaurant import Restaurant
from app.models.user import User


_PROMPT_DIR = Path(__file__).parent


def load_agent_prompt(agent: ChatAgent) -> str:
    path = _PROMPT_DIR / f"{agent.value}.md"
    if not path.exists():
        # Sommelier is the only one shipped in Phase 0; later agents
        # come online with their own files.
        path = _PROMPT_DIR / "sommelier.md"
    return path.read_text(encoding="utf-8")


_PILLAR_LABEL = {
    TastePillar.presentation: "presentación",
    TastePillar.execution: "ejecución técnica",
    TastePillar.value_prop: "costo/beneficio",
}


_WISHLIST_RECALL_LIMIT = 7
"""How many wishlist items we surface to the agent. Seven is enough to
seed recall threads ("¿el risotto que guardaste?") without turning the
prompt into a wall of dish names. Older items first — those are the
ones most worth nudging the comensal about."""


async def build_user_block(
    db: AsyncSession,
    user: User | None,
    profile: UserTasteProfile | None,
) -> str | None:
    """Render the 'Sobre el comensal' addendum to the system prompt.

    Returns ``None`` for anonymous users so we don't paste an empty
    block. For authenticated users without a profile yet, we still emit
    a one-line block with the display name.

    The function is ``async`` because it runs one extra query for the
    wishlist top-7. The query is cheap (compound PK on
    ``want_to_try_dishes`` + per-user limit), and we'd rather pay that
    cost once at prompt-build time than have the agent fire a separate
    tool call to fetch the same data on every recall opportunity.
    """
    if user is None:
        return None

    lines: list[str] = ["# Sobre el comensal"]
    name = user.display_name or user.handle or "el comensal"
    lines.append(f"- Nombre: {name}")

    if profile is None:
        lines.append(
            "- Aún no tenemos suficientes reseñas suyas para inferir "
            "preferencias. No le atribuyas gustos que no haya declarado."
        )
    else:
        if profile.dominant_pillar:
            label = _PILLAR_LABEL[profile.dominant_pillar]
            lines.append(f"- Pondera más alto el pilar de {label}.")
        if profile.top_neighborhoods:
            lines.append(
                "- Zonas donde reseña más seguido: "
                + ", ".join(profile.top_neighborhoods)
                + "."
            )
        if profile.top_categories:
            lines.append(
                "- Categorías favoritas: "
                + ", ".join(profile.top_categories)
                + "."
            )
        if profile.favorite_tags:
            lines.append(
                "- Tags que aparecen en sus reseñas: "
                + ", ".join(profile.favorite_tags)
                + "."
            )
        if profile.avg_price_band:
            lines.append(
                f"- Rango de precio típico: {profile.avg_price_band.value}."
            )
        if profile.preferred_hours:
            lines.append(
                "- Suele comer afuera a las "
                + "h, ".join(str(h) for h in profile.preferred_hours)
                + "h."
            )
        if profile.allergies:
            lines.append(
                "- Restricciones declaradas (RESPETAR SIEMPRE): "
                + ", ".join(profile.allergies)
                + "."
            )

        if len(lines) == 2:  # only the name line
            lines.append(
                "- Sin preferencias inferidas todavía. Saludá por "
                "nombre y preguntá por su pedido sin atribuirle gustos."
            )

    # Wishlist recall — appended for ANY authenticated user (even if
    # ``profile`` is None), because saving items is independent of the
    # taste-profile aggregator.
    wishlist_lines = await _build_wishlist_lines(db, user.id)
    if wishlist_lines:
        lines.append("")  # blank line separator inside the block
        lines.append("## Lista para probar (wishlist)")
        lines.extend(wishlist_lines)
        lines.append(
            "- Mencionalos solo cuando aplique: (a) si saluda y hay "
            "items >30 días sin tachar, ofrecé hacer recall; (b) si "
            "busca en un barrio donde el comensal tiene un item "
            "guardado, mencionálo como contexto; (c) si pregunta "
            "directo qué guardó, listalos. NUNCA recites la lista "
            "entera de golpe ni la repitas en cada turno."
        )

    return "\n".join(lines)


async def _build_wishlist_lines(
    db: AsyncSession, user_id
) -> list[str]:
    """Return the wishlist bullet list for the system prompt, or an
    empty list if the user has nothing saved.

    Pulls the oldest ``_WISHLIST_RECALL_LIMIT`` entries — the comensal
    is most likely to want to be reminded of items that have aged a
    while without being checked off. ``WantToTryDish`` doesn't define
    ORM relationships back to dish/restaurant (just FK columns), so we
    JOIN explicitly here instead of using ``selectinload``.
    """
    stmt = (
        select(WantToTryDish, Dish, Restaurant)
        .join(Dish, WantToTryDish.dish_id == Dish.id)
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .where(WantToTryDish.user_id == user_id)
        .order_by(WantToTryDish.created_at.asc())
        .limit(_WISHLIST_RECALL_LIMIT)
    )
    rows = list((await db.execute(stmt)).all())
    if not rows:
        return []

    out: list[str] = []
    for want, dish, rest in rows:
        rest_name = rest.name if rest is not None else "(sin restaurante)"
        location = rest.location_name if rest is not None else None
        date_iso = want.created_at.date().isoformat()
        loc_fragment = f" — {location}" if location else ""
        out.append(
            f"- *{dish.name}* en **{rest_name}**{loc_fragment} "
            f"(guardado el {date_iso})."
        )
    return out
