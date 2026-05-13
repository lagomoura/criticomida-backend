"""Defensive dish resolution helper shared by every chat agent.

The Business agent already had ``_resolve_dish_in_scope`` in
``business.py``. The Sommelier needs the same shape of contract — accept
``dish_id`` *or* ``dish_name``, return either a clean ``Dish`` or a
structured payload that guides the LLM toward the right next step
(disambiguate, suggest alternatives, ask to register the dish) — but
spanning the whole catalog instead of one restaurant.

Rather than fork the logic twice we expose a single
``_resolve_dish_global`` parameterised by ``restaurant_scope_id``:

- ``restaurant_scope_id=None`` → Sommelier path. Search the entire
  catalog. Candidates carry restaurant + location so the comensal can
  tell two "risotto" entries apart at a glance.
- ``restaurant_scope_id="<uuid>"`` → Business path. Same behavior the
  Business agent had before — search a single menu, peek the rest of
  the menu when there's no match, suggest registering a new dish when
  the menu is empty.

The point of this helper is to make hand-backs structurally impossible:
even if the LLM ignores Regla #0 and dumps a name into ``dish_id`` (or
worse, asks the human for an ID), the tool short-circuits to a useful,
LLM-readable payload instead of a bare error.
"""

from __future__ import annotations

import unicodedata
import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import Dish
from app.models.restaurant import Restaurant


_MAX_CANDIDATES = 12
"""Cap for `candidates` / `menu_peek` payloads. Twelve is enough to
cover real-world ambiguity ("dame todos los risottos del menú")
without flooding the chat surface with an unreadable wall of text."""


_GLOBAL_PRIMARY_LIMIT = 60
"""Upper bound on the SQL ILIKE primary scan. ILIKE is case-insensitive
but NOT accent-insensitive, so it filters quickly on the happy path —
when the user types accents matching the DB."""


_GLOBAL_FALLBACK_LIMIT = 2000
"""Last-resort scan when ILIKE finds nothing (typical when the user
types ``cafe`` and the menu has ``Café``). We re-filter in Python with
accent stripping; 2000 rows is well within memory but high enough to
hit any reasonable city's catalog."""


Actor = Literal["comensal", "owner"]


def _normalize_for_search(text: str) -> str:
    """Strip accents and lowercase for fuzzy substring matching.

    Spanish/Portuguese menus are full of accents and people don't type
    them. NFD decomposition + filtering combining marks (Mn) is the
    canonical Unicode trick — keeps us off the postgres ``unaccent``
    extension and the migration that would entail.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    return stripped.lower().strip()


def _candidate_payload(
    dish: Dish, *, include_restaurant: bool
) -> dict[str, Any]:
    """Render one dish as a candidate row for the LLM.

    For the global Sommelier path, include the restaurant + location so
    the comensal can disambiguate "risotto de hongos" between two
    different restaurants. For the scoped Business path the restaurant
    is implicit (it's the owner's), so we keep the payload tight.
    """
    base: dict[str, Any] = {
        "dish_id": str(dish.id),
        "name": dish.name,
        "review_count": dish.review_count,
        "rating": (
            float(dish.computed_rating)
            if dish.computed_rating is not None
            else None
        ),
    }
    if include_restaurant and dish.restaurant is not None:
        base["restaurant_name"] = dish.restaurant.name
        base["restaurant_slug"] = dish.restaurant.slug
        base["location_name"] = dish.restaurant.location_name
        base["city"] = dish.restaurant.city
    return base


def _actor_words(actor: Actor) -> tuple[str, str]:
    """Return ``(nominativo, dativo)`` so message templates stay
    grammatical in Spanish ('al owner' vs 'a el owner')."""
    if actor == "owner":
        return "el owner", "al owner"
    return "el comensal", "al comensal"


async def _resolve_dish_global(
    db: AsyncSession,
    *,
    restaurant_scope_id: str | None,
    dish_id: str | None,
    dish_name: str | None,
    actor: Actor = "comensal",
) -> tuple[Dish | None, dict[str, Any] | None]:
    """Resolve a dish from a free-form name, a UUID, or both.

    Returns ``(dish, None)`` on a clean resolution. Otherwise returns
    ``(None, payload)`` where ``payload`` is a structured guide the LLM
    should render as a natural message — never as raw JSON.

    Cases covered:

    - **UUID hit**: returns the Dish row. If ``restaurant_scope_id`` is
      set, the dish must belong to it; otherwise any catalog dish.
    - **UUID valid but out of scope / not in catalog**: payload with
      ``error: "dish_not_in_scope"`` (Business) or ``"dish_not_found"``
      (Sommelier). Caller should ask the human what they meant — never
      ask for another UUID.
    - **Name → unique match**: returns the Dish row.
    - **Name → multiple matches**: payload with ``needs_disambiguation:
      True`` and a numbered list. LLM presents the list and waits for
      the user to pick.
    - **Name → zero matches**:
        - Scoped + menu non-empty: ``no_match`` with ``menu_peek``.
        - Scoped + menu empty: ``no_dishes_registered``.
        - Global: ``no_match`` with a hint to fall back to
          ``search_dishes(semantic_query=…)``.
    - **Both inputs missing/empty**: ``missing_input``.
    """
    actor_nom, actor_dat = _actor_words(actor)
    include_restaurant = restaurant_scope_id is None

    # ── Path 1 — explicit UUID ────────────────────────────────────────
    if dish_id:
        try:
            uid = uuid.UUID(dish_id)
        except ValueError:
            # The LLM passed a name in ``dish_id`` (a common slip when
            # Regla #0 doesn't fully land). Treat it as a name.
            dish_name = dish_name or dish_id
        else:
            stmt = (
                select(Dish)
                .options(selectinload(Dish.restaurant))
                .where(Dish.id == uid)
            )
            if restaurant_scope_id is not None:
                stmt = stmt.where(Dish.restaurant_id == restaurant_scope_id)
            dish = (await db.execute(stmt)).scalars().first()
            if dish is not None:
                return dish, None
            # UUID was valid but not found / out of scope.
            if not dish_name:
                if restaurant_scope_id is not None:
                    return None, {
                        "error": "dish_not_in_scope",
                        "message": (
                            "Ese dish_id no pertenece a tu restaurante. "
                            "Si nombraste un plato, pasalo en dish_name "
                            "para que lo busque por nombre."
                        ),
                    }
                return None, {
                    "error": "dish_not_found",
                    "message": (
                        "Ese dish_id no existe en el catálogo. Si "
                        "nombraste un plato, pasalo en dish_name o "
                        "llamá search_dishes para buscarlo."
                    ),
                }

    # ── Path 2 — name search ──────────────────────────────────────────
    if not dish_name or not dish_name.strip():
        return None, {
            "error": "missing_input",
            "message": (
                "Pasame el plato como ``dish_name`` (texto libre, p.ej. "
                "'hamburguesa', 'risotto') o ``dish_id`` (UUID que "
                "viene de search_dishes). NUNCA le pidas "
                f"{actor_dat} el ID."
            ),
        }

    needle = dish_name.strip()
    needle_norm = _normalize_for_search(needle)

    stmt = select(Dish).options(selectinload(Dish.restaurant))
    if restaurant_scope_id is not None:
        # Scoped: pull the whole menu (small) and filter in Python so
        # accents don't trip ILIKE up.
        stmt = stmt.where(Dish.restaurant_id == restaurant_scope_id).order_by(
            Dish.review_count.desc(), Dish.name.asc()
        )
        all_dishes = list((await db.execute(stmt)).scalars().all())
    else:
        # Global: ILIKE first (fast happy path), then a wider scan as
        # fallback for accent-mismatched queries.
        primary_stmt = (
            stmt.where(Dish.name.ilike(f"%{needle}%"))
            .order_by(Dish.review_count.desc(), Dish.name.asc())
            .limit(_GLOBAL_PRIMARY_LIMIT)
        )
        all_dishes = list((await db.execute(primary_stmt)).scalars().all())
        if not all_dishes:
            wider_stmt = (
                select(Dish)
                .options(selectinload(Dish.restaurant))
                .order_by(Dish.review_count.desc(), Dish.name.asc())
                .limit(_GLOBAL_FALLBACK_LIMIT)
            )
            all_dishes = list((await db.execute(wider_stmt)).scalars().all())

    matches = [
        d for d in all_dishes if needle_norm in _normalize_for_search(d.name)
    ]

    if len(matches) == 1:
        return matches[0], None

    if len(matches) > 1:
        return None, {
            "needs_disambiguation": True,
            "query": needle,
            "candidates": [
                _candidate_payload(d, include_restaurant=include_restaurant)
                for d in matches[:_MAX_CANDIDATES]
            ],
            "message": (
                f"Tengo {len(matches)} platos que matchean '{needle}'. "
                f"Mostrale {actor_dat} los candidatos como una lista "
                "numerada y dejá que elija (con número, letra o nombre "
                "completo). Cuando elija, llamá el tool de nuevo con "
                "el dish_id del candidato. NUNCA pidas 'el nombre "
                "exacto' — el humano ya te dijo lo que quería."
            ),
        }

    # ── Zero matches ──────────────────────────────────────────────────
    if restaurant_scope_id is not None:
        if not all_dishes:
            return None, {
                "error": "no_dishes_registered",
                "query": needle,
                "message": (
                    "No tengo ningún plato registrado en este "
                    f"restaurante todavía. Decile {actor_dat} que no hay "
                    f"'{needle}' (ni nada) en su menú y ofrecele "
                    "registrarlo desde el panel del owner."
                ),
            }
        return None, {
            "error": "no_match",
            "query": needle,
            "menu_peek": [
                _candidate_payload(d, include_restaurant=False)
                for d in all_dishes[:_MAX_CANDIDATES]
            ],
            "message": (
                f"No encontré ningún plato que matchee '{needle}'. "
                f"Mostrale {actor_dat} los platos de menu_peek como "
                "lista numerada y preguntale cuál quería (acepta "
                f"número, letra o nombre). Si '{needle}' realmente no "
                "aparece y ningún plato del menú es similar, ofrecele "
                "registrarlo desde el panel del owner. NUNCA pidas "
                "'el nombre exacto' ni el ID — los humanos no hablan "
                "así."
            ),
        }

    # Global path: peeking the whole catalog isn't useful; instead
    # nudge the LLM to fall back to search_dishes' semantic search.
    return None, {
        "error": "no_match",
        "query": needle,
        "message": (
            f"No encontré ningún plato cuyo nombre contenga '{needle}' "
            "en el catálogo de Palato. Llamá `search_dishes` con "
            f"`semantic_query='{needle}'` para que el motor busque por "
            "similitud semántica (puede que el plato exista bajo otro "
            f"nombre), o decile {actor_dat} que no aparece en la base "
            "actual. NUNCA pidas 'el nombre exacto' ni el ID."
        ),
    }


def _restaurant_candidate_payload(rest: Restaurant) -> dict[str, Any]:
    """Render a Restaurant as a candidate row for the LLM.

    Tighter than the dish payload — we include only what the comensal
    needs to disambiguate two restaurants with similar names (location
    + city), plus identifiers the LLM can pass back in the next call.
    """
    return {
        "restaurant_id": str(rest.id),
        "slug": rest.slug,
        "name": rest.name,
        "location_name": rest.location_name,
        "city": rest.city,
    }


async def _resolve_restaurant_global(
    db: AsyncSession,
    *,
    restaurant_id: str | None,
    restaurant_slug: str | None,
    restaurant_name: str | None,
) -> tuple[Restaurant | None, dict[str, Any] | None]:
    """Resolve a restaurant from uuid / slug / free-form name.

    Mirror of :func:`_resolve_dish_global`, adapted to the restaurant
    table. Priority: UUID > slug exact > name fuzzy. Returns
    ``(Restaurant, None)`` on a clean resolution, otherwise ``(None,
    payload)`` with a structured hint the LLM should turn into a
    natural message — never raw JSON.

    Cases covered:

    - **UUID hit**: returns the Restaurant row.
    - **UUID invalid**: treated as a free-form name (same trick used in
      :func:`_resolve_dish_global` to forgive the LLM dropping a name
      into the wrong field).
    - **UUID valid but not in catalog**: falls back to slug/name if
      either is provided, otherwise returns ``restaurant_not_found``.
    - **Slug hit**: returns the Restaurant row.
    - **Slug miss**: falls back to name if provided, otherwise
      ``slug_not_found``.
    - **Name → unique match**: returns the Restaurant row.
    - **Name → multiple matches**: ``needs_disambiguation: True`` with
      a candidate list (capped). LLM presents the list and waits for
      the comensal to pick.
    - **Name → zero matches**: ``no_match`` with a hint to ask the
      comensal to clarify.
    - **Missing input**: defensive ``missing_input`` (the schema's
      ``model_validator`` should catch this first, but defense in
      depth is cheap).
    """
    # ── Path 1 — explicit UUID ────────────────────────────────────────
    if restaurant_id:
        try:
            uid = uuid.UUID(restaurant_id)
        except ValueError:
            # The LLM passed a name in ``restaurant_id``. Treat it as
            # the name input (same forgiveness pattern as dish).
            restaurant_name = restaurant_name or restaurant_id
        else:
            stmt = select(Restaurant).where(Restaurant.id == uid)
            rest = (await db.execute(stmt)).scalars().first()
            if rest is not None:
                return rest, None
            if not restaurant_slug and not restaurant_name:
                return None, {
                    "error": "restaurant_not_found",
                    "message": (
                        "Ese restaurant_id no existe en el catálogo. "
                        "Pasá el nombre libre del lugar en "
                        "``restaurant_name`` o el slug en "
                        "``restaurant_slug``."
                    ),
                }

    # ── Path 2 — slug exact match ─────────────────────────────────────
    if restaurant_slug and restaurant_slug.strip():
        slug = restaurant_slug.strip().lower()
        stmt = select(Restaurant).where(Restaurant.slug == slug)
        rest = (await db.execute(stmt)).scalars().first()
        if rest is not None:
            return rest, None
        if not restaurant_name:
            return None, {
                "error": "slug_not_found",
                "query": slug,
                "message": (
                    f"El slug '{slug}' no existe en el catálogo. Si "
                    "el comensal mencionó el restaurante por nombre, "
                    "pasalo en ``restaurant_name`` y reintento — yo "
                    "resuelvo la ambigüedad."
                ),
            }

    # ── Path 3 — fuzzy name search ────────────────────────────────────
    if not restaurant_name or not restaurant_name.strip():
        return None, {
            "error": "missing_input",
            "message": (
                "Pasame el restaurante como ``restaurant_id`` (UUID), "
                "``restaurant_slug`` o ``restaurant_name`` (texto libre, "
                "p.ej. 'Eretz', 'la cantina israelí'). NUNCA le pidas "
                "al comensal el ID."
            ),
        }

    needle = restaurant_name.strip()
    needle_norm = _normalize_for_search(needle)

    # Primary: ILIKE on name (fast happy path when accents match).
    primary_stmt = (
        select(Restaurant)
        .where(Restaurant.name.ilike(f"%{needle}%"))
        .order_by(Restaurant.review_count.desc(), Restaurant.name.asc())
        .limit(_GLOBAL_PRIMARY_LIMIT)
    )
    candidates = list((await db.execute(primary_stmt)).scalars().all())

    if not candidates:
        # Fallback: wider scan + Python-side accent-insensible filter
        # against name + location_name (so "Eretz Palermo" matches via
        # the barrio token even if the name alone wouldn't).
        wider_stmt = (
            select(Restaurant)
            .order_by(Restaurant.review_count.desc(), Restaurant.name.asc())
            .limit(_GLOBAL_FALLBACK_LIMIT)
        )
        all_rows = list((await db.execute(wider_stmt)).scalars().all())
        candidates = [
            r
            for r in all_rows
            if needle_norm in _normalize_for_search(r.name)
            or needle_norm in _normalize_for_search(r.location_name or "")
        ]
    else:
        # The ILIKE primary scan may include rows where the substring
        # only matches case-insensitively; refine in Python so accent
        # mismatches don't survive.
        candidates = [
            r for r in candidates if needle_norm in _normalize_for_search(r.name)
        ]

    if len(candidates) == 1:
        return candidates[0], None

    if len(candidates) > 1:
        return None, {
            "needs_disambiguation": True,
            "query": needle,
            "candidates": [
                _restaurant_candidate_payload(r)
                for r in candidates[:_MAX_CANDIDATES]
            ],
            "message": (
                f"Tengo {len(candidates)} restaurantes que matchean "
                f"'{needle}'. Mostrale al comensal los candidatos como "
                "una lista numerada (nombre + barrio + ciudad) y dejá "
                "que elija. Cuando aclare, llamá el tool de nuevo con "
                "el restaurant_id o restaurant_slug del candidato. "
                "NUNCA pidas 'el nombre exacto'."
            ),
        }

    return None, {
        "error": "no_match",
        "query": needle,
        "message": (
            f"No encontré ningún restaurante cuyo nombre o barrio "
            f"contenga '{needle}' en el catálogo. Pedile al comensal "
            "que aclare el nombre o que pruebe con el barrio del "
            "lugar. NUNCA pidas 'el nombre exacto' ni el ID."
        ),
    }
