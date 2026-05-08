"""Analytics tools for the Palato Business agent (Phase 3).

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
- ``list_reviews`` — single composable tool over the restaurant's
  reviews. Combines filters (responded status, sentiment) and sort
  order so any review-listing question the owner asks is one tool
  call, no per-question tools.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, asc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chat import DishEmbedding
from app.models.dish import Dish, DishReview, SentimentLabel
from app.models.owner_content import DishReviewOwnerResponse
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._resolution import (
    _normalize_for_search,
    _resolve_dish_global,
)
from app.services.chat.tools._schemas import (
    ListReviewsInput,
    RespondedStatus,
    ReviewSort,
    Sentiment,
    pydantic_to_anthropic_schema,
)


# ──────────────────────────────────────────────────────────────────────────
#   Shared helpers
# ──────────────────────────────────────────────────────────────────────────


async def _resolve_dish_in_scope(
    db: AsyncSession,
    *,
    restaurant_scope_id: str | None,
    dish_id: str | None,
    dish_name: str | None,
) -> tuple[Dish | None, dict[str, Any] | None]:
    """Business-flavored wrapper over the shared resolver.

    Forces ``restaurant_scope_id`` (Business agent always has one) and
    speaks to the LLM with owner-specific phrasing. Lives here so the
    rest of business.py keeps importing it from the same place.
    """
    if restaurant_scope_id is None:
        return None, {"error": "Business scope is required."}
    return await _resolve_dish_global(
        db,
        restaurant_scope_id=restaurant_scope_id,
        dish_id=dish_id,
        dish_name=dish_name,
        actor="owner",
    )


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
        "dish_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Optional. UUID que viene de search_dishes / "
                "rank_my_dishes. Pasalo cuando ya lo tenés."
            ),
        },
        "dish_name": {
            "type": "string",
            "description": (
                "Optional. Nombre o término libre del plato como lo "
                "dijo el owner (p.ej. 'hamburguesa', 'el risotto'). "
                "El tool resuelve el nombre internamente. NUNCA le "
                "pidas al owner el dish_id — pasale el nombre acá."
            ),
        },
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
    "required": ["pillar"],
    "additionalProperties": False,
}


def make_analyze_dish_pillar_drop_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        pillar_key = args.get("pillar")
        if pillar_key not in _PILLAR_COLUMNS:
            return {"error": "Invalid pillar."}

        dish, error = await _resolve_dish_in_scope(
            db,
            restaurant_scope_id=restaurant_scope_id,
            dish_id=args.get("dish_id"),
            dish_name=args.get("dish_name"),
        )
        if error is not None:
            return error
        assert dish is not None  # mypy: guaranteed by the contract above

        dish_id = dish.id
        window_days = int(args.get("window_days", 30))
        pillar_col = _PILLAR_COLUMNS[pillar_key]

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
            "review excerpts so the owner sees what's behind the number. "
            "Pass either `dish_id` (UUID) or `dish_name` (free text the "
            "owner used, like 'hamburguesa' or 'el risotto'). The tool "
            "resolves names internally — if there are multiple matches "
            "it returns candidates for disambiguation; if there are "
            "zero, it returns a peek of the menu so you can offer "
            "alternatives. NEVER ask the owner for an ID."
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
        "dish_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Optional. UUID que viene de search_dishes / "
                "rank_my_dishes. Pasalo cuando ya lo tenés."
            ),
        },
        "dish_name": {
            "type": "string",
            "description": (
                "Optional. Nombre o término libre del plato como lo "
                "dijo el owner (p.ej. 'hamburguesa', 'el risotto'). "
                "El tool resuelve el nombre internamente. NUNCA le "
                "pidas al owner el dish_id — pasale el nombre acá."
            ),
        },
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
        anchor, error = await _resolve_dish_in_scope(
            db,
            restaurant_scope_id=restaurant_scope_id,
            dish_id=args.get("dish_id"),
            dish_name=args.get("dish_name"),
        )
        if error is not None:
            return error
        assert anchor is not None

        radius_km = float(args.get("radius_km", 2))
        limit = int(args.get("limit", 8))
        dish_id = anchor.id

        # Re-load with the restaurant relationship for downstream use.
        anchor = (
            await db.execute(
                select(Dish)
                .where(Dish.id == dish_id)
                .options(selectinload(Dish.restaurant))
            )
        ).scalars().first()
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
        # We exclude the anchor's own restaurant entirely — the owner
        # is asking about *competition*, not their other dishes.
        deg_buffer = radius_km / 80.0
        bbox_filter = and_(
            Dish.restaurant_id != anchor.restaurant_id,
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

        # Single query: bbox filter en SQL + LEFT JOIN al embedding +
        # cosine_distance computado por Postgres usando el índice HNSW
        # (vector_cosine_ops). Antes era N+1 con la distancia coseno
        # calculada a mano en Python.
        if anchor_emb is not None:
            sim_col = DishEmbedding.embedding.cosine_distance(
                anchor_emb.embedding
            ).label("sim_distance")
            cohort_stmt = (
                select(Dish, sim_col)
                .join(Restaurant, Dish.restaurant_id == Restaurant.id)
                .outerjoin(
                    DishEmbedding, DishEmbedding.dish_id == Dish.id
                )
                .where(bbox_filter)
                .options(selectinload(Dish.restaurant))
                .order_by(sim_col.asc().nullslast())
                .limit(200)
            )
            candidates: list[tuple[Dish, float | None]] = [
                (dish, float(sim) if sim is not None else None)
                for dish, sim in (await db.execute(cohort_stmt)).all()
            ]
        else:
            cohort_stmt = (
                select(Dish)
                .join(Restaurant, Dish.restaurant_id == Restaurant.id)
                .where(bbox_filter)
                .options(selectinload(Dish.restaurant))
                .limit(200)
            )
            candidates = [
                (dish, None)
                for dish in (
                    await db.execute(cohort_stmt)
                ).scalars().all()
            ]

        # Haversine refinement post-bbox (PostGIS está fuera de stack).
        scored: list[tuple[Dish, float, float | None]] = []
        for cand, sim_distance in candidates:
            r = cand.restaurant
            dist = _haversine_km(
                float(rest.latitude),
                float(rest.longitude),
                float(r.latitude),
                float(r.longitude),
            )
            if dist > radius_km:
                continue
            scored.append((cand, dist, sim_distance))

        # Cuando hay vectores, el SQL ya ordenó por similaridad — sólo
        # romper empates por distancia física. Sin vectores, ordenamos
        # por cercanía pura.
        if anchor_emb is not None:
            scored.sort(
                key=lambda t: (
                    t[2] if t[2] is not None else float("inf"),
                    t[1],
                )
            )
        else:
            scored.sort(key=lambda t: t[1])
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
            "The cohort EXCLUDES the owner's own restaurant — this is "
            "competition only, never your other dishes. Returns the "
            "percentile rank of the dish's rating in the cohort plus "
            "the closest comparable dishes ordered by semantic "
            "similarity. Requires the dish's restaurant to have "
            "lat/lng populated. Pass either `dish_id` (UUID) or "
            "`dish_name` (free text the owner used, like 'hamburguesa' "
            "or 'el risotto'). The tool resolves names internally — if "
            "there are multiple matches it returns candidates for "
            "disambiguation; if there are zero, it returns a peek of "
            "the menu so you can offer alternatives. NEVER ask the "
            "owner for an ID."
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
#   list_reviews — single composable tool for any review-listing question
# ──────────────────────────────────────────────────────────────────────────


# Contract is enforced by ``ListReviewsInput`` (Pydantic). Provider-side
# enum validation rejects out-of-range values before the call ever lands
# here; if one slips through, ``model_validate`` raises and the agent loop
# surfaces the error to the model so it can retry. We do not maintain
# synonym tables — natural-language → enum is the LLM's job, in any
# language.


def make_list_reviews_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        try:
            inputs = ListReviewsInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for list_reviews.",
                "details": exc.errors(include_url=False),
            }

        sentiment_filter = (
            SentimentLabel(inputs.sentiment.value)
            if inputs.sentiment is not Sentiment.any
            else None
        )

        stmt = (
            select(DishReview, Dish, DishReviewOwnerResponse.review_id)
            .join(Dish, DishReview.dish_id == Dish.id)
            .outerjoin(
                DishReviewOwnerResponse,
                DishReviewOwnerResponse.review_id == DishReview.id,
            )
            .where(Dish.restaurant_id == restaurant_scope_id)
        )

        if inputs.responded_status is RespondedStatus.pending:
            stmt = stmt.where(DishReviewOwnerResponse.review_id.is_(None))
        elif inputs.responded_status is RespondedStatus.responded:
            stmt = stmt.where(DishReviewOwnerResponse.review_id.is_not(None))

        if sentiment_filter is not None:
            stmt = stmt.where(DishReview.sentiment_label == sentiment_filter)

        applied: dict[str, Any] = {
            "responded_status": inputs.responded_status.value,
            "sentiment": inputs.sentiment.value,
            "sort": inputs.sort.value,
            "limit": inputs.limit,
        }

        if inputs.dish_name_contains and inputs.dish_name_contains.strip():
            applied["dish_name_contains"] = inputs.dish_name_contains
            all_dishes = list(
                (
                    await db.execute(
                        select(Dish.id, Dish.name).where(
                            Dish.restaurant_id == restaurant_scope_id
                        )
                    )
                ).all()
            )
            needle = _normalize_for_search(inputs.dish_name_contains)
            matching_ids = [
                row.id
                for row in all_dishes
                if needle in _normalize_for_search(row.name)
            ]
            if not matching_ids:
                # Empty-result branch: no dishes match the filter. We tell
                # the LLM what happened so it can suggest the menu instead
                # of inventing platos. This is a *factual* status, not a
                # tool error — the call succeeded with zero rows.
                return {
                    "restaurant_id": restaurant_scope_id,
                    "count": 0,
                    "applied_filters": applied,
                    "no_dish_matched": True,
                    "reviews": [],
                }
            stmt = stmt.where(DishReview.dish_id.in_(matching_ids))

        if inputs.min_rating is not None:
            stmt = stmt.where(DishReview.rating >= inputs.min_rating)
            applied["min_rating"] = inputs.min_rating
        if inputs.max_rating is not None:
            stmt = stmt.where(DishReview.rating <= inputs.max_rating)
            applied["max_rating"] = inputs.max_rating

        if inputs.date_from is not None:
            stmt = stmt.where(func.date(DishReview.created_at) >= inputs.date_from)
            applied["date_from"] = inputs.date_from.isoformat()
        if inputs.date_to is not None:
            stmt = stmt.where(func.date(DishReview.created_at) <= inputs.date_to)
            applied["date_to"] = inputs.date_to.isoformat()

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
                DishReview.rating.desc(),
                DishReview.created_at.desc(),
            )
        elif inputs.sort is ReviewSort.rating_low:
            stmt = stmt.order_by(
                DishReview.rating.asc(),
                DishReview.created_at.desc(),
            )
        elif inputs.sort is ReviewSort.oldest:
            stmt = stmt.order_by(DishReview.created_at.asc())
        else:  # ReviewSort.recent
            stmt = stmt.order_by(DishReview.created_at.desc())

        rows = list((await db.execute(stmt.limit(inputs.limit))).all())

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
                "has_owner_response": resp_id is not None,
                "excerpt": (rev.note or "")[:240],
            }
            for rev, dish, resp_id in rows
        ]

        return {
            "restaurant_id": restaurant_scope_id,
            "count": len(items),
            "applied_filters": applied,
            "reviews": items,
        }

    return ToolSpec(
        name="list_reviews",
        description=(
            "Single tool for ANY question about reviews of this "
            "restaurant. Compose filters: ``responded_status``, "
            "``sentiment``, ``dish_name_contains`` (substring "
            "accent-insensitive), ``min_rating``/``max_rating`` (1-5), "
            "``date_from``/``date_to`` (ISO YYYY-MM-DD). Order with "
            "``sort``. Each parameter accepts only the enum values "
            "documented in its schema — translate the owner's natural "
            "language into the right enum yourself, in any language. "
            "Examples: 'última review' → sort='recent', limit=1; "
            "'reseñas duras de abril' → date_from='2026-04-01', "
            "date_to='2026-04-30', sort='most_negative'. The response "
            "always includes ``applied_filters`` so you can see exactly "
            "what ran. Pick the loosest filters the owner asked for — "
            "don't invent constraints."
        ),
        input_schema=pydantic_to_anthropic_schema(ListReviewsInput),
        handler=handler,
    )
