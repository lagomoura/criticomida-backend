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
from sqlalchemy import and_, asc, func, select
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
from app.services.chat.tools._resolution import (
    _normalize_for_search,
    _resolve_dish_global,
    _resolve_restaurant_global,
)
from app.services.chat.tools._schemas import (
    GetDishDetailInput,
    ListRestaurantReviewsInput,
    ReviewSort,
    SearchDishesInput,
    Sentiment,
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
        # Business agent hard-pin: NEVER lifted, even if the LLM passes
        # a different ``restaurant_id`` in the args. Owner cross-talk is
        # the threat model — assert at SQL boundary.
        conditions.append(Restaurant.id == restaurant_scope_id)
    elif inputs.restaurant_id:
        # Sommelier soft-pin: the LLM extracted a restaurant from the
        # Context Injection hint or from a previous tool output. Cast
        # to UUID defensively — if the LLM dumped a name here, the cast
        # raises and the agent loop surfaces the ValidationError back
        # to the model for retry (Regla #0 still applies; the LLM
        # should switch to ``neighborhood``/``semantic_query`` or a
        # ``get_dish_detail`` call to get the right uuid).
        try:
            conditions.append(Restaurant.id == uuid.UUID(inputs.restaurant_id))
        except ValueError:
            return {
                "error": (
                    "Invalid restaurant_id (not a UUID). Did you mean to "
                    "pass a free-text name? Use ``get_dish_detail`` to "
                    "resolve the restaurant first or drop the parameter."
                ),
                "count": 0,
                "dishes": [],
                "semantic_used": False,
            }

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

    # Hard name filter — acento-insensible, contra la columna stored
    # ``dishes.name_normalized`` (lower + f_unaccent, generated column,
    # indexada por gin_trgm). Le permite al agente garantizar que si el
    # comensal pidió "ceviche" el resultado contiene "ceviche" — el
    # re-ranking semántico solo no alcanza cuando embeddings tienen
    # ruido o están sin generar para platos nuevos.
    if inputs.name_contains:
        needle_norm = _normalize_for_search(inputs.name_contains)
        if needle_norm:
            conditions.append(
                Dish.name_normalized.ilike(f"%{needle_norm}%")
            )

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
            "tier, name_contains) with an optional semantic_query for "
            "vibe-based ranking. Hard filters are AND and never relaxed. "
            "**Pidió un plato por nombre concreto** ('ceviche', 'ramen', "
            "'milanesa', 'café'): pasá ``name_contains=<nombre>`` — "
            "filtro SQL acento-insensible contra la columna normalizada "
            "del nombre, garantiza que el resultado contiene ese plato. "
            "El ``semantic_query`` solo no alcanza ahí: embeddings con "
            "ruido pueden rankear platos no relacionados sobre el match "
            "real. **Data-only**: the rows come back to YOU; the comensal "
            "does NOT see them as cards. After reading the results, "
            "decide which 1-6 actually answer the question and call "
            "``recommend_dishes(dish_ids=[...])`` to present that "
            "curated subset. Skipping the recommend_dishes step means "
            "the comensal sees nothing — search_dishes alone is "
            "invisible to them."
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


# ──────────────────────────────────────────────────────────────────────────
#   list_restaurant_reviews — Sommelier parametric listing of public reviews
# ──────────────────────────────────────────────────────────────────────────


def make_list_restaurant_reviews_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
) -> ToolSpec:
    """Build ``list_restaurant_reviews`` (Sommelier-only).

    B2C mirror of ``list_reviews`` (Business). Two key differences:

    - **Dynamic scope**: resolves the restaurant from
      ``restaurant_id`` / ``restaurant_slug`` / ``restaurant_name``
      (free text) — the comensal never knows ids. The shared resolver
      handles disambiguation, slug lookup, and graceful fallbacks.
    - **Anonymous output**: no ``user_id`` / display name is exposed.
      Reviews from the public catalog are read by the comensal
      anonymously — consistent with ``get_dish_detail``.

    ``user_id`` (optional) enables the safety filter against
    blocked/muted authors. Applied at SQL level via
    ``excluded_author_ids_subquery``.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from app.models.dish import DishReview, SentimentLabel
        from app.services.safety_service import excluded_author_ids_subquery

        # 1. Pydantic validation (extra=forbid + model_validator)
        try:
            inputs = ListRestaurantReviewsInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for list_restaurant_reviews.",
                "details": exc.errors(include_url=False),
            }

        # 2. Crossed ranges — return useful error, not exception. The
        # LLM reads the message and retries with corrected args in the
        # same turn (agent loop pattern).
        if (
            inputs.min_rating is not None
            and inputs.max_rating is not None
            and inputs.min_rating > inputs.max_rating
        ):
            return {
                "error": "invalid_rating_range",
                "message": (
                    f"min_rating ({inputs.min_rating}) no puede ser "
                    f"mayor que max_rating ({inputs.max_rating}). "
                    "Volvé a llamar con valores correctos."
                ),
            }
        if (
            inputs.date_from is not None
            and inputs.date_to is not None
            and inputs.date_from > inputs.date_to
        ):
            return {
                "error": "invalid_date_range",
                "message": (
                    f"date_from ({inputs.date_from.isoformat()}) no "
                    "puede ser posterior a date_to "
                    f"({inputs.date_to.isoformat()})."
                ),
            }

        # 3. Resolve restaurant. Resolver returns a structured hint
        # payload on ambiguity / no_match so the LLM can guide the
        # comensal without falling into hand-back patterns.
        restaurant, hint = await _resolve_restaurant_global(
            db,
            restaurant_id=inputs.restaurant_id,
            restaurant_slug=inputs.restaurant_slug,
            restaurant_name=inputs.restaurant_name,
        )
        if hint is not None:
            return hint
        assert restaurant is not None

        # 4. Build SELECT — eager-load pros_cons so we can include them
        # per review without an N+1 burst. Reviews per restaurant are
        # capped by the LIMIT below, so the batched fetch stays small.
        sentiment_filter = (
            SentimentLabel(inputs.sentiment.value)
            if inputs.sentiment is not Sentiment.any
            else None
        )

        stmt = (
            select(DishReview, Dish)
            .join(Dish, DishReview.dish_id == Dish.id)
            .where(Dish.restaurant_id == restaurant.id)
            .options(selectinload(DishReview.pros_cons))
        )

        if sentiment_filter is not None:
            stmt = stmt.where(DishReview.sentiment_label == sentiment_filter)
        if inputs.min_rating is not None:
            stmt = stmt.where(DishReview.rating >= inputs.min_rating)
        if inputs.max_rating is not None:
            stmt = stmt.where(DishReview.rating <= inputs.max_rating)
        if inputs.date_from is not None:
            stmt = stmt.where(
                func.date(DishReview.created_at) >= inputs.date_from
            )
        if inputs.date_to is not None:
            stmt = stmt.where(
                func.date(DishReview.created_at) <= inputs.date_to
            )

        if inputs.dish_name_contains and inputs.dish_name_contains.strip():
            needle_norm = _normalize_for_search(inputs.dish_name_contains)
            if needle_norm:
                stmt = stmt.where(
                    Dish.name_normalized.ilike(f"%{needle_norm}%")
                )

        # Safety filter at SQL level — efficient because the base query
        # is already restaurant-scoped and we don't need a post-query
        # reload (unlike get_dish_detail, which works in memory).
        if user_id is not None:
            stmt = stmt.where(
                DishReview.user_id.notin_(
                    excluded_author_ids_subquery(user_id)
                )
            )

        if inputs.sort is ReviewSort.most_negative:
            stmt = stmt.order_by(
                asc(DishReview.sentiment_score).nullslast(),
                DishReview.created_at.desc(),
            )
        elif inputs.sort is ReviewSort.most_positive:
            stmt = stmt.order_by(
                DishReview.sentiment_score.desc().nullslast(),
                DishReview.created_at.desc(),
            )
        elif inputs.sort is ReviewSort.rating_high:
            stmt = stmt.order_by(
                DishReview.rating.desc(), DishReview.created_at.desc()
            )
        elif inputs.sort is ReviewSort.rating_low:
            stmt = stmt.order_by(
                DishReview.rating.asc(), DishReview.created_at.desc()
            )
        elif inputs.sort is ReviewSort.oldest:
            stmt = stmt.order_by(DishReview.created_at.asc())
        else:  # ReviewSort.recent
            stmt = stmt.order_by(DishReview.created_at.desc())

        rows = list((await db.execute(stmt.limit(inputs.limit))).all())

        # 5. Build ``applied_filters`` for the LLM to echo back when
        # narrating ("una de N reseñas en abril con sentimiento
        # negativo"). Mirror of the Business list_reviews shape so the
        # agent loop sees a familiar payload across agents.
        applied: dict[str, Any] = {
            "sentiment": inputs.sentiment.value,
            "sort": inputs.sort.value,
            "limit": inputs.limit,
        }
        if inputs.min_rating is not None:
            applied["min_rating"] = inputs.min_rating
        if inputs.max_rating is not None:
            applied["max_rating"] = inputs.max_rating
        if inputs.dish_name_contains:
            applied["dish_name_contains"] = inputs.dish_name_contains
        if inputs.date_from is not None:
            applied["date_from"] = inputs.date_from.isoformat()
        if inputs.date_to is not None:
            applied["date_to"] = inputs.date_to.isoformat()

        def _excerpt(note: str | None) -> str:
            if not note:
                return ""
            # Collapse whitespace so the LLM gets clean text without
            # stray newlines / double spaces.
            return " ".join(note.split())[:240]

        items = [
            {
                "review_id": str(rev.id),
                "dish_id": str(dish.id),
                "dish_name": dish.name,
                "created_at": rev.created_at.isoformat(),
                "rating": (
                    float(rev.rating) if rev.rating is not None else None
                ),
                "presentation": rev.presentation,
                "execution": rev.execution,
                "value_prop": rev.value_prop,
                "would_order_again": rev.would_order_again,
                "meal_period": (
                    rev.meal_period.value
                    if rev.meal_period is not None
                    else None
                ),
                "sentiment_label": (
                    rev.sentiment_label.value
                    if rev.sentiment_label is not None
                    else None
                ),
                "sentiment_score": (
                    float(rev.sentiment_score)
                    if rev.sentiment_score is not None
                    else None
                ),
                "excerpt": _excerpt(rev.note),
                "pros": [
                    pc.text for pc in rev.pros_cons if pc.type.value == "pro"
                ][:3],
                "cons": [
                    pc.text for pc in rev.pros_cons if pc.type.value == "con"
                ][:3],
            }
            for rev, dish in rows
        ]

        return {
            "restaurant": {
                "id": str(restaurant.id),
                "slug": restaurant.slug,
                "name": restaurant.name,
                "location_name": restaurant.location_name,
                "city": restaurant.city,
                "rating": (
                    float(restaurant.computed_rating)
                    if restaurant.computed_rating is not None
                    else None
                ),
                "review_count": restaurant.review_count,
            },
            "count": len(items),
            "applied_filters": applied,
            "reviews": items,
        }

    return ToolSpec(
        name="list_restaurant_reviews",
        description=(
            "Listado paramétrico de reseñas de un restaurante del "
            "catálogo. Usalo cuando el comensal pregunta por opiniones, "
            "quejas, mejor/peor reseña, sentimiento o experiencias en "
            "un lugar concreto ('¿cuál es la peor reseña de Eretz?', "
            "'¿qué se está quejando la gente últimamente?', "
            "'reseñas negativas del último mes'). Composable: combiná "
            "``sentiment``, ``sort`` (incluye ``most_negative`` / "
            "``most_positive``), rangos de rating y fecha, "
            "``dish_name_contains`` (substring acento-insensible para "
            "acotar a un plato). Identificá el restaurante por "
            "``restaurant_id`` (UUID, viene de search_dishes), "
            "``restaurant_slug`` (también del output previo) o "
            "``restaurant_name`` (texto libre como lo dijo el comensal); "
            "si hay ambigüedad el tool devuelve candidatos para que "
            "aclares. **Output anónimo** — no expone autor; hablá en "
            "tercera persona ('hay una reseña que dice...'). "
            "**Cuándo NO usar**: si es descubrimiento general ('algo "
            "rico en Palermo') → search_dishes; si es detalle de UN "
            "plato con pros/cons agregados → get_dish_detail. Esta tool "
            "es para preguntas centradas en lo que opina el público "
            "sobre un lugar concreto."
        ),
        input_schema=pydantic_to_anthropic_schema(ListRestaurantReviewsInput),
        handler=handler,
        emits_card=False,
    )
