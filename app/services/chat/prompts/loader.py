"""System prompt loader.

Each agent has a markdown file in this folder. The loader reads the
file and optionally appends a "Sobre el comensal" block built from the
authenticated user's taste profile so the bot can greet by name and
respect declared allergies.
"""

from __future__ import annotations

from pathlib import Path

from app.models.chat import ChatAgent, TastePillar, UserTasteProfile
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


def build_user_block(
    user: User | None, profile: UserTasteProfile | None
) -> str | None:
    """Render the 'Sobre el comensal' addendum to the system prompt.

    Returns ``None`` for anonymous users so we don't paste an empty
    block. For authenticated users without a profile yet, we still emit
    a one-line block with the display name.
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
        return "\n".join(lines)

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
            "- Categorías favoritas: " + ", ".join(profile.top_categories) + "."
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
            "- Sin preferencias inferidas todavía. Saludá por nombre y "
            "preguntá por su pedido sin atribuirle gustos."
        )

    return "\n".join(lines)
