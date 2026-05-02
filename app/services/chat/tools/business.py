"""Analytics tools for the CritiComida Business agent (Phase 3).

All tools are scoped to the verified owner's restaurant. The
``restaurant_scope_id`` set on the conversation is enforced both here
and at the registry: an owner can never lift the scope by tweaking
arguments on the wire.

Three tools shipped:

- ``analyze_dish_pillar_drop`` — diagnoses why a pillar score dropped.
  Compares the pillar average over the last ``window_days`` against
  the previous window of equal length and returns the delta plus the
  most negative review notes that mention pillar-relevant keywords.
- ``benchmark_dish`` — finds dishes within ``radius_km`` whose
  embedding is closest to ``dish_id`` (semantic peers) and computes
  percentile ranks for each pillar across that cohort.
- ``list_pending_reviews`` — lightweight wrapper over the existing
  ``owner_content`` view: returns reviews on this restaurant that the
  owner hasn't responded to yet.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chat import DishEmbedding
from app.models.dish import Dish, DishReview
from app.models.owner_content import DishReviewOwnerResponse
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec


# ──────────────────────────────────────────────────────────────────────────
#   analyze_dish_pillar_drop
# ──────────────────────────────────────────────────────────────────────────


_PILLAR_COLUMNS = {
    "presentation": DishReview.presentation,
    "execution": DishReview.execution,
    "value_prop": DishReview.value_prop,
}

# Keywords that flag a *negative* mention of each pillar in the review
# note. Spanish-leaning because that's what the corpus speaks today.
_NEGATIVE_KEYWORDS = {
    "presentation": (
        "presentaci", "feo", "descuid", "pobre", "deslucid", "sin gracia",
    ),
    "execution": (
        "crud", "quemad", "fri", "duro", "blanduch", "pasad", "salad", "sos",
        "soso", "insipid", "deshecho",
    ),
    "value_prop": (
        "caro", "barato", "porci", "chico", "no vale", "no rinde", "sobreprec",
    ),
}


ANALYZE_PILLAR_DROP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dish_id": {"type": "string", "format": "uuid"},
        "pillar": {
            "type": "string",
            "enum": ["presentation", "execution", "value_prop"],
        },
        "window_days": {
            "type": "integer",
            "minimum": 7,
            "maximum": 180,
            "default": 30,
        },
    },
    "required": ["dish_id", "pillar"],
    "additionalProperties": False,
}


def make_analyze_dish_pillar_drop_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        dish_id = uuid.UUID(args["dish_id"])
        pillar_key = args["pillar"]
        window_days = int(args.get("window_days", 30))
        pillar_col = _PILLAR_COLUMNS[pillar_key]

        # Defense in depth: the dish must belong to the scoped restaurant.
        dish = (
            await db.execute(
                select(Dish).where(
                    and_(
                        Dish.id == dish_id,
                        Dish.restaurant_id == restaurant_scope_id,
                    )
                )
            )
        ).scalars().first()
        if dish is None:
            return {"error": "Dish not found on this restaurant."}

        now = datetime.now(timezone.utc)
        recent_start = now - timedelta(days=window_days)
        prior_start = now - timedelta(days=window_days * 2)

        async def _avg(
            since: datetime, until: datetime
        ) -> tuple[float | None, int]:
            row = (
                await db.execute(
                    select(
                        func.avg(pillar_col).label("avg"),
                        func.count(pillar_col).label("n"),
                    ).where(
                        and_(
                            DishReview.dish_id == dish_id,
                            DishReview.created_at >= since,
                            DishReview.created_at < until,
                            pillar_col.is_not(None),
                        )
                    )
                )
            ).one()
            avg = float(row.avg) if row.avg is not None else None
            return avg, int(row.n)

        recent_avg, recent_n = await _avg(recent_start, now)
        prior_avg, prior_n = await _avg(prior_start, recent_start)

        delta: float | None = None
        if recent_avg is not None and prior_avg is not None:
            delta = round(recent_avg - prior_avg, 2)

        # Pull recent reviews that mention pillar-relevant negativity.
        keywords = _NEGATIVE_KEYWORDS[pillar_key]
        keyword_conditions = [
            func.lower(DishReview.note).contains(kw) for kw in keywords
        ]
        # Also include reviews that simply scored the pillar low (1).
        score_condition = pillar_col == 1
        recent_negative_stmt = (
            select(DishReview)
            .where(
                and_(
                    DishReview.dish_id == dish_id,
                    DishReview.created_at >= recent_start,
                    or_(score_condition, *keyword_conditions),
                )
            )
            .order_by(DishReview.created_at.desc())
            .limit(5)
        )
        negative_rows = list(
            (await db.execute(recent_negative_stmt)).scalars().all()
        )
        snippets = [
            {
                "review_id": str(r.id),
                "created_at": r.created_at.isoformat(),
                "rating": float(r.rating) if r.rating is not None else None,
                "pillar_score": getattr(r, pillar_key),
                "excerpt": (r.note or "")[:280],
            }
            for r in negative_rows
            if r.note
        ]

        return {
            "dish_id": str(dish_id),
            "dish_name": dish.name,
            "pillar": pillar_key,
            "window_days": window_days,
            "recent_avg": (
                round(recent_avg, 2) if recent_avg is not None else None
            ),
            "recent_count": recent_n,
            "prior_avg": (
                round(prior_avg, 2) if prior_avg is not None else None
            ),
            "prior_count": prior_n,
            "delta": delta,
            "negative_snippets": snippets,
        }

    return ToolSpec(
        name="analyze_dish_pillar_drop",
        description=(
            "Diagnose a drop on a single dish pillar. Compares the "
            "average over the last `window_days` against the prior "
            "equal-length window and returns the most recent negative "
            "review excerpts so the owner sees what's behind the number."
        ),
        input_schema=ANALYZE_PILLAR_DROP_SCHEMA,
        handler=handler,
    )


# ──────────────────────────────────────────────────────────────────────────
#   benchmark_dish
# ──────────────────────────────────────────────────────────────────────────


BENCHMARK_DISH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dish_id": {"type": "string", "format": "uuid"},
        "radius_km": {
            "type": "number",
            "minimum": 0.2,
            "maximum": 25,
            "default": 2,
        },
        "limit": {
            "type": "integer",
            "minimum": 3,
            "maximum": 20,
            "default": 8,
        },
    },
    "required": ["dish_id"],
    "additionalProperties": False,
}


def _haversine_km(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """Crude great-circle distance — good enough for 2-25 km filtering.
    pgvector + a haversine SQL function would be more elegant, but the
    cohort sizes here are small enough to filter in Python."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _percentile(values: list[float], target: float) -> float:
    """Percentile rank of ``target`` within ``values`` (inclusive)."""
    if not values:
        return 0.0
    below = sum(1 for v in values if v < target)
    eq = sum(1 for v in values if v == target)
    return round(100 * (below + 0.5 * eq) / len(values), 1)


def make_benchmark_dish_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        dish_id = uuid.UUID(args["dish_id"])
        radius_km = float(args.get("radius_km", 2))
        limit = int(args.get("limit", 8))

        # Anchor dish + parent restaurant.
        anchor = (
            await db.execute(
                select(Dish)
                .where(
                    and_(
                        Dish.id == dish_id,
                        Dish.restaurant_id == restaurant_scope_id,
                    )
                )
                .options(selectinload(Dish.restaurant))
            )
        ).scalars().first()
        if anchor is None:
            return {"error": "Dish not found on this restaurant."}

        rest = anchor.restaurant
        if rest.latitude is None or rest.longitude is None:
            return {
                "error": (
                    "Restaurant has no coordinates yet — geographic "
                    "benchmark unavailable."
                )
            }

        anchor_emb = (
            await db.execute(
                select(DishEmbedding).where(DishEmbedding.dish_id == dish_id)
            )
        ).scalars().first()

        # Build candidate cohort: dishes whose restaurant is inside a
        # generous square (the haversine filter trims it precisely).
        # 0.012 deg ≈ 1.3 km at temperate latitudes — fine as a coarse cap.
        deg_buffer = radius_km / 80.0
        cohort_stmt = (
            select(Dish)
            .join(Restaurant, Dish.restaurant_id == Restaurant.id)
            .where(
                and_(
                    Dish.id != dish_id,
                    Restaurant.latitude.is_not(None),
                    Restaurant.longitude.is_not(None),
                    Restaurant.latitude.between(
                        float(rest.latitude) - deg_buffer,
                        float(rest.latitude) + deg_buffer,
                    ),
                    Restaurant.longitude.between(
                        float(rest.longitude) - deg_buffer,
                        float(rest.longitude) + deg_buffer,
                    ),
                )
            )
            .options(selectinload(Dish.restaurant))
            .limit(200)
        )
        candidates = list((await db.execute(cohort_stmt)).scalars().all())

        # Trim to within radius and rank by embedding distance.
        scored: list[tuple[Dish, float, float | None]] = []
        for cand in candidates:
            r = cand.restaurant
            dist = _haversine_km(
                float(rest.latitude),
                float(rest.longitude),
                float(r.latitude),
                float(r.longitude),
            )
            if dist > radius_km:
                continue
            sim_distance: float | None = None
            if anchor_emb is not None:
                cand_emb = (
                    await db.execute(
                        select(DishEmbedding).where(
                            DishEmbedding.dish_id == cand.id
                        )
                    )
                ).scalars().first()
                if cand_emb is not None:
                    sim_distance = float(
                        sum(
                            (a - b) * (a - b)
                            for a, b in zip(
                                anchor_emb.embedding,
                                cand_emb.embedding,
                                strict=False,
                            )
                        )
                        ** 0.5
                    )
            scored.append((cand, dist, sim_distance))

        # Order: nearest semantic neighbours first when we have vectors,
        # otherwise nearest physical neighbours.
        scored.sort(
            key=lambda t: (t[2] if t[2] is not None else float("inf"), t[1])
        )
        cohort = scored[:limit]

        # Percentile of the anchor on each pillar across the cohort.
        cohort_ratings = [
            float(c.computed_rating)
            for c, *_ in cohort
            if c.computed_rating is not None
        ]
        anchor_rating = (
            float(anchor.computed_rating)
            if anchor.computed_rating is not None
            else None
        )
        rating_percentile = (
            _percentile(cohort_ratings, anchor_rating)
            if anchor_rating is not None
            else None
        )

        peers = [
            {
                "dish_id": str(c.id),
                "name": c.name,
                "restaurant_name": c.restaurant.name,
                "restaurant_slug": c.restaurant.slug,
                "distance_km": round(dist, 2),
                "rating": (
                    float(c.computed_rating)
                    if c.computed_rating is not None
                    else None
                ),
                "review_count": c.review_count,
                "semantic_distance": (
                    round(sim_dist, 4) if sim_dist is not None else None
                ),
            }
            for c, dist, sim_dist in cohort
        ]

        return {
            "dish_id": str(anchor.id),
            "dish_name": anchor.name,
            "anchor_rating": anchor_rating,
            "anchor_review_count": anchor.review_count,
            "radius_km": radius_km,
            "cohort_size": len(cohort),
            "rating_percentile": rating_percentile,
            "peers": peers,
            "semantic_used": anchor_emb is not None,
        }

    return ToolSpec(
        name="benchmark_dish",
        description=(
            "Compare a dish against semantic peers within a radius. "
            "Returns the percentile rank of the dish's rating in the "
            "cohort plus the closest comparable dishes ordered by "
            "semantic similarity. Requires the dish's restaurant to "
            "have lat/lng populated."
        ),
        input_schema=BENCHMARK_DISH_SCHEMA,
        handler=handler,
        # Vector reads + many JOINs: give it a bit more time.
        timeout_seconds=15.0,
    )


# ──────────────────────────────────────────────────────────────────────────
#   rank_my_dishes
# ──────────────────────────────────────────────────────────────────────────


_RANK_SORT_OPTIONS = {
    "rating": "rating",
    "review_count": "review_count",
    "presentation": "presentation",
    "execution": "execution",
    "value_prop": "value_prop",
}


RANK_MY_DISHES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sort_by": {
            "type": "string",
            "enum": list(_RANK_SORT_OPTIONS.keys()),
            "default": "rating",
            "description": (
                "Field to rank by. ``rating`` is the aggregate computed "
                "rating; ``review_count`` ranks by volume; the three "
                "pillars rank by their per-dish average."
            ),
        },
        "order": {
            "type": "string",
            "enum": ["desc", "asc"],
            "default": "desc",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
            "default": 10,
        },
        "min_review_count": {
            "type": "integer",
            "minimum": 0,
            "default": 1,
            "description": (
                "Drop dishes with fewer reviews than this. Useful so a "
                "single 5-star review doesn't crown a brand-new dish."
            ),
        },
    },
    "additionalProperties": False,
}


def make_rank_my_dishes_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        sort_by = args.get("sort_by", "rating")
        order = args.get("order", "desc")
        limit = int(args.get("limit", 10))
        min_count = int(args.get("min_review_count", 1))

        # Pre-aggregate the three pillar averages per dish in a single
        # round trip — avoids a join blow-up if a dish has many reviews.
        pillar_avgs = (
            select(
                DishReview.dish_id.label("dish_id"),
                func.avg(DishReview.presentation).label("avg_presentation"),
                func.avg(DishReview.execution).label("avg_execution"),
                func.avg(DishReview.value_prop).label("avg_value_prop"),
            )
            .group_by(DishReview.dish_id)
            .subquery()
        )

        sort_column = {
            "rating": Dish.computed_rating,
            "review_count": Dish.review_count,
            "presentation": pillar_avgs.c.avg_presentation,
            "execution": pillar_avgs.c.avg_execution,
            "value_prop": pillar_avgs.c.avg_value_prop,
        }[sort_by]
        order_clause = (
            sort_column.desc().nullslast()
            if order == "desc"
            else sort_column.asc().nullsfirst()
        )

        stmt = (
            select(
                Dish,
                pillar_avgs.c.avg_presentation,
                pillar_avgs.c.avg_execution,
                pillar_avgs.c.avg_value_prop,
            )
            .outerjoin(pillar_avgs, pillar_avgs.c.dish_id == Dish.id)
            .where(
                and_(
                    Dish.restaurant_id == restaurant_scope_id,
                    Dish.review_count >= min_count,
                )
            )
            .order_by(order_clause, Dish.review_count.desc())
            .limit(limit)
        )
        rows = list((await db.execute(stmt)).all())

        items: list[dict[str, Any]] = []
        for dish, p_pres, p_exec, p_value in rows:
            items.append(
                {
                    "dish_id": str(dish.id),
                    "name": dish.name,
                    "rating": (
                        float(dish.computed_rating)
                        if dish.computed_rating is not None
                        else None
                    ),
                    "review_count": dish.review_count,
                    "price_tier": (
                        dish.price_tier.value if dish.price_tier else None
                    ),
                    "avg_presentation": (
                        round(float(p_pres), 2) if p_pres is not None else None
                    ),
                    "avg_execution": (
                        round(float(p_exec), 2) if p_exec is not None else None
                    ),
                    "avg_value_prop": (
                        round(float(p_value), 2) if p_value is not None else None
                    ),
                }
            )

        return {
            "restaurant_id": restaurant_scope_id,
            "sort_by": sort_by,
            "order": order,
            "min_review_count": min_count,
            "count": len(items),
            "dishes": items,
        }

    return ToolSpec(
        name="rank_my_dishes",
        description=(
            "Rank the dishes of this restaurant by rating, review "
            "volume, or any of the three pillars (presentation, "
            "execution, value_prop). Use it when the owner asks about "
            "their best/worst plato, top sellers, or which dishes need "
            "attention. Filters out dishes with fewer than "
            "``min_review_count`` reviews to avoid crowning untested "
            "items."
        ),
        input_schema=RANK_MY_DISHES_SCHEMA,
        handler=handler,
    )


# ──────────────────────────────────────────────────────────────────────────
#   list_pending_reviews
# ──────────────────────────────────────────────────────────────────────────


LIST_PENDING_REVIEWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
            "default": 10,
        },
    },
    "additionalProperties": False,
}


def make_list_pending_reviews_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        limit = int(args.get("limit", 10))

        # Reviews on this restaurant's dishes that don't have an owner
        # response yet. We left-join the response table and keep rows
        # where it's NULL.
        stmt = (
            select(DishReview, Dish)
            .join(Dish, DishReview.dish_id == Dish.id)
            .outerjoin(
                DishReviewOwnerResponse,
                DishReviewOwnerResponse.review_id == DishReview.id,
            )
            .where(
                and_(
                    Dish.restaurant_id == restaurant_scope_id,
                    DishReviewOwnerResponse.review_id.is_(None),
                )
            )
            .order_by(DishReview.created_at.desc())
            .limit(limit)
        )
        rows = list((await db.execute(stmt)).all())

        items = [
            {
                "review_id": str(rev.id),
                "dish_id": str(dish.id),
                "dish_name": dish.name,
                "created_at": rev.created_at.isoformat(),
                "rating": float(rev.rating) if rev.rating is not None else None,
                "presentation": rev.presentation,
                "execution": rev.execution,
                "value_prop": rev.value_prop,
                "excerpt": (rev.note or "")[:240],
            }
            for rev, dish in rows
        ]
        return {
            "restaurant_id": restaurant_scope_id,
            "pending_count": len(items),
            "reviews": items,
        }

    return ToolSpec(
        name="list_pending_reviews",
        description=(
            "List recent reviews on this restaurant that the owner has "
            "not responded to yet. Use it when the owner asks 'what's "
            "pending', 'cuáles me faltan responder', etc."
        ),
        input_schema=LIST_PENDING_REVIEWS_SCHEMA,
        handler=handler,
    )
