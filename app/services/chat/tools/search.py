"""Search tools exposed to the agent.

``search_dishes`` is the discovery primitive: the LLM extracts
structured filters from the user's request (neighborhood, pillar minima,
bbox, price tier, category) and optionally a free-text ``semantic_query``
for "vibe" matching. We apply the structured filters as SQL WHERE clauses
*first*, then re-rank within that subset by cosine distance against
``dish_embeddings`` if a semantic query is present.

This pre-filter approach is the standard pgvector pattern: it guarantees
hard constraints ("Palermo", "value_prop=3") are respected and lets the
embedding decide order *within* the subset.

**Important — search_dishes is data-only.** It does NOT emit cards to
the comensal. The agent reads the rows, decides which 1-6 actually
answer the question, and calls ``recommend_dishes(dish_ids=[...])``
to present the curated subset. Splitting these responsibilities is
what keeps the visible grid in sync with the editorial text — see
``tools/recommend.py`` for the reasoning.

``get_dish_detail`` is also data-only — it serves the agent context
to write a deeper paragraph about a single plato.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.chat import DishEmbedding
from app.models.dish import Dish, PriceTier
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._allergy_filter import (
    filter_dishes_by_allergies,
    get_user_allergies,
)
from app.services.chat.tools._resolution import _resolve_dish_global
from app.services.chat.tools._schemas import (
    GetDishDetailInput,
    SearchDishesInput,
    pydantic_to_anthropic_schema,
)


_PRICE_TIER_RANK = {PriceTier.low: 1, PriceTier.mid: 2, PriceTier.high: 3}


def _serialize_dish(
    dish: Dish, *, saved_ids: set[Any] | None = None
) -> dict[str, Any]:
    """Render a Dish as the JSON shape the FE consumes.

    ``saved_ids`` is an optional set of dish UUIDs the comensal has
    already added to their want-to-try list. When provided, the
    response includes a ``want_to_try`` boolean per dish so the FE
    can paint the bookmark state correctly on first render — without
    it, the chip resets to "Quiero probar" on every refresh even for
    dishes that are already saved server-side. We accept ``set[Any]``
    rather than ``set[UUID]`` so callers can pass either UUID
    instances or strings; ``dish.id`` membership is what matters.
    """
    restaurant = dish.restaurant
    return {
        "dish_id": str(dish.id),
        "name": dish.name,
        "description": dish.description,
        "cover_image_url": dish.cover_image_url,
        "rating": float(dish.computed_rating) if dish.computed_rating else None,
        "review_count": dish.review_count,
        "price_tier": dish.price_tier.value if dish.price_tier else None,
        # Always present so the FE can rely on the field; ``False``
        # when no auth context (anonymous comensal) — the bookmark
        # write would 401 anyway, so the bookmark state is moot.
        "want_to_try": (saved_ids is not None and dish.id in saved_ids),
        "restaurant": {
            "id": str(restaurant.id),
            "slug": restaurant.slug,
            "name": restaurant.name,
            "location_name": restaurant.location_name,
            "city": restaurant.city,
            "lat": float(restaurant.latitude) if restaurant.latitude else None,
            "lng": float(restaurant.longitude) if restaurant.longitude else None,
            "category": (
                restaurant.category.name if restaurant.category else None
            ),
            "has_reservation": restaurant.has_reservation,
            "is_claimed": restaurant.is_claimed,
        },
    }


async def execute_dish_search(
    db: AsyncSession,
    *,
    inputs: SearchDishesInput,
    restaurant_scope_id: str | None = None,
    user_id: uuid.UUID | None = None,
    query_vector: list[float] | None = None,
) -> dict[str, Any]:
    """Filtered + optional KNN-ranked dish search, shared kernel.

    Extracted from the ``search_dishes`` tool so callers that ALREADY
    have a query vector skip the text-embedding step and pass their
    pre-computed vector directly. The motivating consumer is
    ``identify_dish_from_photo``: it embeds the comensal's photo with
    ``embed_image`` (Gemini Embedding 2 multimodal — same vector
    space as ``dish_embeddings``) and feeds the resulting vector
    here. Plain text consumers like ``search_dishes`` keep their
    original flow: embed_query → vector → this helper.

    All ``SearchDishesInput`` filters apply as SQL WHERE (AND, never
    relaxed). When ``query_vector`` is provided, the result set is
    re-ranked by cosine distance against ``dish_embeddings``;
    otherwise we fall back to ``computed_rating, review_count``.

    The allergy guard runs unconditionally so unsafe dishes never
    reach the caller, regardless of which ranking path took them
    here.
    """
    from app.models.dish import DishReview  # local import to avoid cycle

    stmt = (
        select(Dish)
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .options(
            selectinload(Dish.restaurant).selectinload(Restaurant.category),
        )
    )

    conditions: list[Any] = []

    if restaurant_scope_id:
        conditions.append(Restaurant.id == restaurant_scope_id)

    if inputs.neighborhood:
        conditions.append(
            Restaurant.location_name.ilike(f"%{inputs.neighborhood}%")
        )

    if inputs.city:
        conditions.append(func.lower(Restaurant.city) == inputs.city.lower())

    if inputs.bbox is not None:
        bbox = inputs.bbox
        conditions.append(Restaurant.latitude.is_not(None))
        conditions.append(Restaurant.longitude.is_not(None))
        conditions.append(Restaurant.latitude.between(bbox.south, bbox.north))
        conditions.append(Restaurant.longitude.between(bbox.west, bbox.east))

    if inputs.min_rating is not None:
        conditions.append(Dish.computed_rating >= inputs.min_rating)

    if inputs.max_price_tier is not None:
        target = PriceTier(inputs.max_price_tier.value)
        allowed = [
            pt for pt, rank in _PRICE_TIER_RANK.items()
            if rank <= _PRICE_TIER_RANK[target]
        ]
        conditions.append(
            Dish.price_tier.in_(allowed) | Dish.price_tier.is_(None)
        )

    if inputs.category_slug:
        stmt = stmt.join(
            Category, Restaurant.category_id == Category.id, isouter=False
        )
        conditions.append(Category.slug == inputs.category_slug)

    # Pillar minima are stored on dish_reviews (one-to-many). We pull
    # dishes whose *latest* review meets the minimum on each requested
    # pillar via correlated EXISTS so we don't accidentally fan-out.
    pillar_filters = (
        ("min_value_prop", DishReview.value_prop, inputs.min_value_prop),
        ("min_presentation", DishReview.presentation, inputs.min_presentation),
        ("min_execution", DishReview.execution, inputs.min_execution),
    )
    for _, col, value in pillar_filters:
        if value is None:
            continue
        subq = (
            select(DishReview.id)
            .where(DishReview.dish_id == Dish.id)
            .where(col >= int(value))
            .limit(1)
        )
        conditions.append(subq.exists())

    if conditions:
        stmt = stmt.where(and_(*conditions))

    if query_vector is not None:
        stmt = (
            stmt.join(
                DishEmbedding,
                DishEmbedding.dish_id == Dish.id,
                isouter=True,
            )
            .order_by(
                DishEmbedding.embedding.cosine_distance(query_vector).asc().nullslast(),
                Dish.computed_rating.desc(),
            )
            .limit(inputs.limit)
        )
    else:
        stmt = stmt.order_by(
            Dish.computed_rating.desc(), Dish.review_count.desc()
        ).limit(inputs.limit)

    result = await db.execute(stmt)
    dishes = list(result.scalars().unique().all())

    # Allergy guard: drop any unsafe dish BEFORE the agent reads the
    # rows. The agent treats the search output as ground truth, so
    # showing it a Malabi-with-nuez when the user is nut-allergic
    # invites the model to either include it (bad) or self-censor
    # the entire answer ("no encontré ningún postre que esté libre
    # de nueces"). Surfacing only safe candidates lets the agent
    # recommend confidently from a pre-filtered set; we still
    # surface ``allergy_drops`` so it can frame the answer
    # ("descarté X y Y por tu restricción de nueces"). The
    # downstream ``recommend_dishes`` filter stays in place as a
    # second layer.
    allergies = await get_user_allergies(db, user_id=user_id)
    kept_dishes, dropped = filter_dishes_by_allergies(dishes, allergies)

    payload: dict[str, Any] = {
        "count": len(kept_dishes),
        "dishes": [_serialize_dish(d) for d in kept_dishes],
        # ``semantic_used`` reflects whether KNN actually ran: true iff
        # we had a usable vector. Honest about degraded paths (Gemini
        # down → no vector → rating-fallback → semantic_used=false).
        "semantic_used": query_vector is not None,
    }
    if dropped:
        payload["allergy_drops"] = dropped
        payload["respected_allergies"] = allergies
        # Explicit instruction for the agent. Production bug: Flash
        # Lite saw ``allergy_drops`` and self-censored ("no encontré
        # postres registrados como libres de nueces") even when
        # ``dishes`` still had safe candidates — it treated the
        # partial drop as evidence of unsafe data instead of
        # confirmation that filtering happened.
        if kept_dishes:
            payload["safe_subset_note"] = (
                "Los dishes que aparecen en ``dishes`` YA pasaron "
                "el filtro de alergias del comensal — son seguros. "
                "Recomendá normalmente desde este subset llamando "
                "``recommend_dishes`` con sus dish_ids. NO digas "
                "'no encontré platos libres de X': eso es falso, "
                "los que están en la lista lo son. Mencioná los "
                "drops sólo si suma editorialmente (ej. 'descarté "
                "el Malabi por las nueces, pero el Kanafeh es "
                "seguro')."
            )
        else:
            payload["safe_subset_note"] = (
                "Después de filtrar por las alergias declaradas, "
                "no quedó NINGÚN plato seguro de los que matchean "
                "los filtros de búsqueda. Decílo en texto y "
                "ofrecé buscar en otra cocina/categoría/zona; NO "
                "llames recommend_dishes con un set vacío."
            )
    return payload


def make_search_dishes_tool(
    db: AsyncSession,
    *,
    embed_query: Any | None = None,
    restaurant_scope_id: str | None = None,
    user_id: uuid.UUID | None = None,
) -> ToolSpec:
    """Build the ``search_dishes`` tool bound to a DB session.

    ``embed_query`` is an async callable ``(text) -> list[float]`` used
    when ``semantic_query`` is present. We accept it as a dep injection
    so tests can stub it without hitting Gemini.

    ``restaurant_scope_id`` (optional) hard-pins the search to a single
    restaurant — used by the Business agent so an owner can never query
    competitors through this tool.

    ``user_id`` (optional) enables the server-side allergy guard: any
    dish whose name/description mentions one of the comensal's
    declared allergens (or a synonym/plural of it) is dropped before
    the rows reach the agent. Without this the agent saw unsafe
    dishes in ``search_dishes`` and would self-censor with a "no
    encontré nada" answer instead of recommending the safe ones.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            inputs = SearchDishesInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for search_dishes.",
                "details": exc.errors(include_url=False),
            }

        # Embed the semantic_query (if any) up front, then delegate
        # the SQL build + KNN + allergy filter to the shared kernel.
        # Multimodal callers (identify_dish_from_photo) skip this
        # block entirely and call execute_dish_search directly with
        # an image-derived vector.
        query_vector: list[float] | None = None
        if inputs.semantic_query and embed_query is not None:
            try:
                query_vector = await embed_query(inputs.semantic_query)
            except Exception:
                query_vector = None

        return await execute_dish_search(
            db,
            inputs=inputs,
            restaurant_scope_id=restaurant_scope_id,
            user_id=user_id,
            query_vector=query_vector,
        )

    return ToolSpec(
        name="search_dishes",
        description=(
            "Search the Palato catalog of dishes. Combine structured "
            "filters (neighborhood, pillar minima, bbox, category, price "
            "tier) with an optional semantic_query for vibe-based ranking. "
            "Hard filters are AND and never relaxed. **Data-only**: the "
            "rows come back to YOU; the comensal does NOT see them as "
            "cards. After reading the results, decide which 1-6 actually "
            "answer the question and call ``recommend_dishes(dish_ids="
            "[...])`` to present that curated subset. Skipping the "
            "recommend_dishes step means the comensal sees nothing — "
            "search_dishes alone is invisible to them."
        ),
        input_schema=pydantic_to_anthropic_schema(SearchDishesInput),
        handler=handler,
        emits_card=False,
    )


# ──────────────────────────────────────────────────────────────────────────
#   get_dish_detail
# ──────────────────────────────────────────────────────────────────────────


def make_get_dish_detail_tool(
    db: AsyncSession,
    *,
    restaurant_scope_id: str | None = None,
    user_id: uuid.UUID | None = None,
) -> ToolSpec:
    """Build the ``get_dish_detail`` tool.

    Accepts ``dish_id`` (UUID) or ``dish_name`` (free text). The shared
    resolver does the heavy lifting — disambiguation, menu peek, fallback
    suggestions — so the LLM never has to ask the human for an ID.

    The Business agent passes ``restaurant_scope_id`` so detail lookups
    can't leak across restaurants; the Sommelier leaves it None and
    searches the whole catalog.

    ``user_id`` (optional, Sommelier-only) habilita el filtro de safety
    sobre los ``top_reviews``: cuando el comensal autenticado bloqueó o
    muteó al autor de una reseña, el texto NO llega al LLM (cierra el
    caveat anotado en docs/ia_services.md). El Business deliberadamente
    no pasa ``user_id`` — la tool ahí sirve para diagnóstico de pilares,
    no para consumo social.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from app.models.dish import DishReview
        from app.services.safety_service import (
            excluded_author_ids_subquery,
        )

        try:
            inputs = GetDishDetailInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for get_dish_detail.",
                "details": exc.errors(include_url=False),
            }

        actor = "owner" if restaurant_scope_id is not None else "comensal"
        dish, error = await _resolve_dish_global(
            db,
            restaurant_scope_id=restaurant_scope_id,
            dish_id=inputs.dish_id,
            dish_name=inputs.dish_name,
            actor=actor,
        )
        if error is not None:
            return error
        assert dish is not None  # contract guaranteed by the resolver

        # Re-load with reviews + pros/cons attached. The resolver only
        # eagerly loads ``restaurant`` (it doesn't know which fields the
        # caller cares about), so we widen the selectinload here.
        dish = (
            await db.execute(
                select(Dish)
                .where(Dish.id == dish.id)
                .options(
                    selectinload(Dish.restaurant).selectinload(
                        Restaurant.category
                    ),
                    selectinload(Dish.reviews).selectinload(
                        DishReview.pros_cons
                    ),
                )
            )
        ).scalars().first()
        if dish is None:  # race: deleted between resolver and reload
            return {"error": "Dish disappeared mid-call. Try again."}

        # Safety filter: si el viewer está autenticado, dropear las reviews
        # de autores bloqueados/muteados antes de que el LLM las vea.
        # Trabajamos en memoria sobre el resultado del selectinload —
        # invertirlo en SQL requeriría re-armar la query desde cero. La
        # cantidad de reviews por plato es chica (decenas como mucho), así
        # que un set-membership en Python alcanza.
        reviews = list(dish.reviews)
        if user_id is not None and reviews:
            excluded_rows = await db.execute(
                excluded_author_ids_subquery(user_id)
            )
            excluded_ids = {row[0] for row in excluded_rows.all()}
            reviews = [r for r in reviews if r.user_id not in excluded_ids]

        top_reviews = sorted(
            reviews, key=lambda r: float(r.rating or 0), reverse=True
        )[:3]
        return {
            **_serialize_dish(dish),
            "reviews": [
                {
                    "rating": float(r.rating) if r.rating else None,
                    "presentation": r.presentation,
                    "execution": r.execution,
                    "value_prop": r.value_prop,
                    "note": r.note,
                    "pros": [
                        pc.text for pc in r.pros_cons if pc.type.value == "pro"
                    ][:3],
                    "cons": [
                        pc.text for pc in r.pros_cons if pc.type.value == "con"
                    ][:3],
                }
                for r in top_reviews
            ],
        }

    return ToolSpec(
        name="get_dish_detail",
        description=(
            "Fetch full information for a single dish: aggregated "
            "pillars, top reviews, pros/cons. Accepts ``dish_id`` (UUID) "
            "OR ``dish_name`` (free text the human used, like 'el risotto' "
            "or 'la pizza margherita'). The tool resolves names internally "
            "— if there are multiple matches it returns candidates for "
            "disambiguation; if there are zero, it suggests fallback to "
            "search_dishes(semantic_query=...). NEVER ask the human for "
            "an ID or 'the exact name'."
        ),
        input_schema=pydantic_to_anthropic_schema(GetDishDetailInput),
        handler=handler,
    )
