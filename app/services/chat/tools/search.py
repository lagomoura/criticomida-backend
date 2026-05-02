"""Search tools exposed to the agent.

``search_dishes`` is the workhorse of the Sommelier: the LLM extracts
structured filters from the user's request (neighborhood, pillar minima,
bbox, price tier, category) and optionally a free-text ``semantic_query``
for "vibe" matching. We apply the structured filters as SQL WHERE clauses
*first*, then re-rank within that subset by cosine distance against
``dish_embeddings`` if a semantic query is present.

This pre-filter approach is the standard pgvector pattern: it guarantees
hard constraints ("Palermo", "value_prop=3") are respected and lets the
embedding decide order *within* the subset.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.chat import DishEmbedding
from app.models.dish import Dish, PriceTier
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec


SEARCH_DISHES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "neighborhood": {
            "type": "string",
            "description": (
                "Substring of the restaurant's location_name to filter by, "
                "e.g. 'Palermo' or 'Centro'. Case-insensitive."
            ),
        },
        "city": {
            "type": "string",
            "description": "Exact city name, e.g. 'Buenos Aires'.",
        },
        "bbox": {
            "type": "object",
            "description": (
                "Geographic bounding box. Use when the user references a "
                "concrete area visible on the map."
            ),
            "properties": {
                "south": {"type": "number"},
                "west": {"type": "number"},
                "north": {"type": "number"},
                "east": {"type": "number"},
            },
            "required": ["south", "west", "north", "east"],
        },
        "min_value_prop": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "description": (
                "Minimum CritiComida cost/benefit pillar (1-3). 3 = 'ganga'."
            ),
        },
        "min_presentation": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "description": "Minimum presentation pillar (1-3).",
        },
        "min_execution": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "description": "Minimum technical execution pillar (1-3).",
        },
        "min_rating": {
            "type": "number",
            "minimum": 0,
            "maximum": 5,
            "description": "Minimum aggregated dish rating.",
        },
        "max_price_tier": {
            "type": "string",
            "enum": ["$", "$$", "$$$"],
            "description": "Cap on the dish price tier.",
        },
        "category_slug": {
            "type": "string",
            "description": "Restaurant category slug, e.g. 'italiana'.",
        },
        "semantic_query": {
            "type": "string",
            "description": (
                "Free-text 'vibe' to re-rank semantically: 'cita romántica', "
                "'comida confort', 'plato sorprendente'. Optional."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 12,
            "default": 6,
        },
    },
    "additionalProperties": False,
}


_PRICE_TIER_RANK = {PriceTier.low: 1, PriceTier.mid: 2, PriceTier.high: 3}


def _serialize_dish(dish: Dish) -> dict[str, Any]:
    restaurant = dish.restaurant
    return {
        "dish_id": str(dish.id),
        "name": dish.name,
        "description": dish.description,
        "cover_image_url": dish.cover_image_url,
        "rating": float(dish.computed_rating) if dish.computed_rating else None,
        "review_count": dish.review_count,
        "price_tier": dish.price_tier.value if dish.price_tier else None,
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


def make_search_dishes_tool(
    db: AsyncSession,
    *,
    embed_query: Any | None = None,
    restaurant_scope_id: str | None = None,
) -> ToolSpec:
    """Build the ``search_dishes`` tool bound to a DB session.

    ``embed_query`` is an async callable ``(text) -> list[float]`` used
    when ``semantic_query`` is present. We accept it as a dep injection
    so tests can stub it without hitting Gemini.

    ``restaurant_scope_id`` (optional) hard-pins the search to a single
    restaurant — used by the Business agent so an owner can never query
    competitors through this tool.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 6))
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

        if neighborhood := args.get("neighborhood"):
            conditions.append(Restaurant.location_name.ilike(f"%{neighborhood}%"))

        if city := args.get("city"):
            conditions.append(func.lower(Restaurant.city) == city.lower())

        bbox = args.get("bbox")
        if bbox:
            conditions.append(Restaurant.latitude.is_not(None))
            conditions.append(Restaurant.longitude.is_not(None))
            conditions.append(Restaurant.latitude.between(bbox["south"], bbox["north"]))
            conditions.append(
                Restaurant.longitude.between(bbox["west"], bbox["east"])
            )

        if (mr := args.get("min_rating")) is not None:
            conditions.append(Dish.computed_rating >= mr)

        if (mpt := args.get("max_price_tier")) is not None:
            try:
                target = PriceTier(mpt)
                allowed = [
                    pt for pt, rank in _PRICE_TIER_RANK.items()
                    if rank <= _PRICE_TIER_RANK[target]
                ]
                conditions.append(
                    Dish.price_tier.in_(allowed) | Dish.price_tier.is_(None)
                )
            except ValueError:
                pass

        if cat_slug := args.get("category_slug"):
            stmt = stmt.join(
                Category, Restaurant.category_id == Category.id, isouter=False
            )
            conditions.append(Category.slug == cat_slug)

        # Pillar minima are stored on dish_reviews (one-to-many). We pull
        # dishes whose *latest* review meets the minimum on each requested
        # pillar via correlated EXISTS so we don't accidentally fan-out.
        from app.models.dish import DishReview  # local import to avoid cycle

        for arg_name, col in (
            ("min_value_prop", DishReview.value_prop),
            ("min_presentation", DishReview.presentation),
            ("min_execution", DishReview.execution),
        ):
            v = args.get(arg_name)
            if v is None:
                continue
            subq = (
                select(DishReview.id)
                .where(DishReview.dish_id == Dish.id)
                .where(col >= int(v))
                .limit(1)
            )
            conditions.append(subq.exists())

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Semantic re-ranking (only if we got an embed_query callable AND
        # the LLM asked for it).
        semantic_query = args.get("semantic_query")
        if semantic_query and embed_query is not None:
            try:
                vec = await embed_query(semantic_query)
            except Exception:
                vec = None
            if vec is not None:
                stmt = (
                    stmt.join(
                        DishEmbedding,
                        DishEmbedding.dish_id == Dish.id,
                        isouter=True,
                    )
                    .order_by(
                        DishEmbedding.embedding.cosine_distance(vec).asc().nullslast(),
                        Dish.computed_rating.desc(),
                    )
                    .limit(limit)
                )
            else:
                stmt = stmt.order_by(
                    Dish.computed_rating.desc(), Dish.review_count.desc()
                ).limit(limit)
        else:
            stmt = stmt.order_by(
                Dish.computed_rating.desc(), Dish.review_count.desc()
            ).limit(limit)

        result = await db.execute(stmt)
        dishes = list(result.scalars().unique().all())

        return {
            "count": len(dishes),
            "dishes": [_serialize_dish(d) for d in dishes],
            "semantic_used": bool(semantic_query and embed_query is not None),
        }

    return ToolSpec(
        name="search_dishes",
        description=(
            "Search the CritiComida catalog of dishes. Combine structured "
            "filters (neighborhood, pillar minima, bbox, category, price) "
            "with an optional semantic_query for vibe-based ranking. "
            "Returns dish cards the UI will render."
        ),
        input_schema=SEARCH_DISHES_SCHEMA,
        handler=handler,
        emits_card=True,
    )


# ──────────────────────────────────────────────────────────────────────────
#   get_dish_detail
# ──────────────────────────────────────────────────────────────────────────


GET_DISH_DETAIL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dish_id": {"type": "string", "format": "uuid"},
    },
    "required": ["dish_id"],
    "additionalProperties": False,
}


def make_get_dish_detail_tool(db: AsyncSession) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        from app.models.dish import DishReview, DishReviewProsCons

        dish_id = args["dish_id"]
        stmt = (
            select(Dish)
            .where(Dish.id == dish_id)
            .options(
                selectinload(Dish.restaurant).selectinload(Restaurant.category),
                selectinload(Dish.reviews).selectinload(DishReview.pros_cons),
            )
        )
        result = await db.execute(stmt)
        dish = result.scalars().first()
        if dish is None:
            return {"error": "Dish not found"}

        top_reviews = sorted(
            dish.reviews, key=lambda r: float(r.rating or 0), reverse=True
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
            "Fetch full information for a single dish: aggregated pillars, "
            "top reviews, pros/cons. Use after the user picks one from a "
            "search_dishes result."
        ),
        input_schema=GET_DISH_DETAIL_SCHEMA,
        handler=handler,
    )
