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
- ``list_reviews`` — single composable tool over the restaurant's
  reviews. Combines filters (responded status, sentiment) and sort
  order so any review-listing question the owner asks is one tool
  call, no per-question tools.
"""

from __future__ import annotations

import math
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, asc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chat import DishEmbedding
from app.models.dish import Dish, DishReview, SentimentLabel
from app.models.owner_content import DishReviewOwnerResponse
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec


# ──────────────────────────────────────────────────────────────────────────
#   Shared helpers
# ──────────────────────────────────────────────────────────────────────────


_MAX_MENU_PEEK = 12


def _normalize_for_search(text: str) -> str:
    """Strip accents and lowercase. So 'cafe' matches 'Café Turco'.

    Spanish menus are full of accents and the owner won't type them.
    Doing this in Python keeps us off the postgres ``unaccent`` extension
    dependency (one less migration to ship) — the per-restaurant dish
    count is small enough that pulling them all and filtering in memory
    is cheap.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    return stripped.lower().strip()


async def _resolve_dish_in_scope(
    db: AsyncSession,
    *,
    restaurant_scope_id: str | None,
    dish_id: str | None,
    dish_name: str | None,
) -> tuple[Dish | None, dict[str, Any] | None]:
    """Resolve a dish in the current restaurant scope from either an
    explicit UUID or a free-form name the owner spoke.

    Returns ``(dish, None)`` on a clean resolution, or ``(None, payload)``
    where ``payload`` is a structured tool result the caller should
    return as-is. The payload guides the LLM toward the right next step
    (disambiguate, suggest alternatives, register a new dish) instead
    of failing with a bare error.

    This is the **defensive contract** that the prompt rule is the
    backup of: even if the LLM ignores the prompt and dumps a name into
    ``dish_id`` (or worse, asks the human for an ID), the tool itself
    short-circuits to a useful response.
    """
    if restaurant_scope_id is None:
        return None, {"error": "Business scope is required."}

    # Path 1 — explicit UUID. Validate that the dish exists AND is in
    # the scoped restaurant.
    if dish_id:
        try:
            uid = uuid.UUID(dish_id)
        except ValueError:
            # Caller passed a name in dish_id (common LLM mistake when
            # the rule fails). Fall through to the name path below.
            dish_name = dish_name or dish_id
        else:
            dish = (
                await db.execute(
                    select(Dish).where(
                        and_(
                            Dish.id == uid,
                            Dish.restaurant_id == restaurant_scope_id,
                        )
                    )
                )
            ).scalars().first()
            if dish is not None:
                return dish, None
            # UUID was valid but not in scope — fall through to name
            # search if a name was also provided, else return clean
            # not-found so the LLM doesn't loop on the same ID.
            if not dish_name:
                return None, {
                    "error": "dish_not_in_scope",
                    "message": (
                        "Ese dish_id no pertenece a tu restaurante. "
                        "Si nombraste un plato, pasalo en dish_name "
                        "para que lo busque por nombre."
                    ),
                }

    # Path 2 — name search. Accent + case insensitive substring on the
    # dish name, scoped to the restaurant. We pull ALL dishes in scope
    # and filter in memory: per-restaurant menus are small (rarely >100
    # rows) and Python normalization is cheaper than wiring postgres
    # ``unaccent`` everywhere.
    if not dish_name or not dish_name.strip():
        return None, {
            "error": "missing_input",
            "message": (
                "Pasame el plato como ``dish_name`` (texto libre, "
                "p.ej. 'hamburguesa', 'risotto') o ``dish_id`` (UUID "
                "que viene de search_dishes). NUNCA le pidas al "
                "owner el ID."
            ),
        }

    needle = dish_name.strip()
    needle_norm = _normalize_for_search(needle)
    all_dishes = list(
        (
            await db.execute(
                select(Dish)
                .where(Dish.restaurant_id == restaurant_scope_id)
                .order_by(Dish.review_count.desc(), Dish.name.asc())
            )
        )
        .scalars()
        .all()
    )
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
                {
                    "dish_id": str(d.id),
                    "name": d.name,
                    "review_count": d.review_count,
                    "rating": (
                        float(d.computed_rating)
                        if d.computed_rating is not None
                        else None
                    ),
                }
                for d in matches[:_MAX_MENU_PEEK]
            ],
            "message": (
                f"Tengo {len(matches)} platos que matchean "
                f"'{needle}'. Mostrale los candidatos al owner como "
                "una lista numerada y dejá que elija (con número, "
                "letra o nombre completo). Cuando elija, llamá el "
                "tool de nuevo con el dish_id del candidato elegido. "
                "NO pidas 'el nombre exacto' — el humano ya te dijo "
                "lo que quería."
            ),
        }

    # Zero matches — show the menu so the LLM has alternatives.
    if not all_dishes:
        return None, {
            "error": "no_dishes_registered",
            "query": needle,
            "message": (
                f"No tengo ningún plato registrado en este "
                f"restaurante todavía. Decile al owner que no hay "
                f"'{needle}' (ni nada) en su menú y ofrecele "
                "registrarlo desde el panel del owner."
            ),
        }
    return None, {
        "error": "no_match",
        "query": needle,
        "menu_peek": [
            {
                "dish_id": str(d.id),
                "name": d.name,
                "review_count": d.review_count,
            }
            for d in all_dishes[:_MAX_MENU_PEEK]
        ],
        "message": (
            f"No encontré ningún plato que matchee '{needle}'. "
            "Mostrale al owner los platos de menu_peek como lista "
            "numerada y preguntale cuál quería (acepta número, letra "
            f"o nombre). Si '{needle}' realmente no aparece y ningún "
            "plato del menú es similar, ofrecele registrarlo desde el "
            "panel del owner. NUNCA pidas 'el nombre exacto' ni el "
            "ID — los humanos no hablan así."
        ),
    }


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
        cohort_stmt = (
            select(Dish)
            .join(Restaurant, Dish.restaurant_id == Restaurant.id)
            .where(
                and_(
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


# Synonym tables — accept generously, normalise to canonical. The LLM
# (and humans triggering it) speak natural language; the tool meets
# them where they are instead of failing a strict enum check.

_RESPONDED_SYNONYMS: dict[str, str] = {
    "any": "any", "all": "any", "todas": "any", "todos": "any", "todas_las": "any", "all_reviews": "any",
    "pending": "pending", "unanswered": "pending", "unresponded": "pending",
    "without_response": "pending", "no_response": "pending",
    "sin_responder": "pending", "sin_respuesta": "pending", "pendientes": "pending",
    "responded": "responded", "answered": "responded", "replied": "responded",
    "with_response": "responded", "respondidas": "responded",
}

_SENTIMENT_SYNONYMS: dict[str, str] = {
    "any": "any", "all": "any", "cualquiera": "any", "todas": "any",
    "positive": "positive", "happy": "positive", "good": "positive", "positivas": "positive",
    "neutral": "neutral", "mixed": "neutral", "neutras": "neutral", "neutrales": "neutral",
    "negative": "negative", "bad": "negative", "harsh": "negative",
    "negativas": "negative", "malas": "negative",
}

_SORT_SYNONYMS: dict[str, str] = {
    "recent": "recent", "newest": "recent", "latest": "recent", "newest_first": "recent",
    "last": "recent", "last_review": "recent", "lastest": "recent",
    "most_recent": "recent", "mas_reciente": "recent", "mas_recientes": "recent",
    "ultima": "recent", "ultimo": "recent", "ultimas": "recent", "ultimos": "recent",
    "oldest": "oldest", "earliest": "oldest", "first": "oldest", "first_review": "oldest",
    "mas_vieja": "oldest", "mas_antigua": "oldest", "primera": "oldest", "primero": "oldest",
    "rating_high": "rating_high", "best_rating": "rating_high", "highest_rated": "rating_high",
    "best": "rating_high", "top_rated": "rating_high", "highest": "rating_high",
    "mejor_calificacion": "rating_high", "mejores": "rating_high", "mejor": "rating_high",
    "rating_low": "rating_low", "worst_rating": "rating_low", "lowest_rated": "rating_low",
    "lowest": "rating_low", "peor_calificacion": "rating_low",
    "most_negative": "most_negative", "harshest": "most_negative", "worst": "most_negative",
    "mas_duras": "most_negative", "peores": "most_negative", "negative_first": "most_negative",
    "most_positive": "most_positive", "happiest": "most_positive", "warmest": "most_positive",
    "best_sentiment": "most_positive", "mas_calidas": "most_positive", "positive_first": "most_positive",
}


def _norm_key(s: str) -> str:
    """Lowercase, replace spaces / hyphens / accents so synonyms match."""
    return _normalize_for_search(s).replace(" ", "_").replace("-", "_")


LIST_REVIEWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "responded_status": {
            "type": "string",
            "default": "any",
            "description": (
                "Filtrá por respuesta del owner. Acepta: 'any', "
                "'pending' (sin responder, también: unanswered, "
                "sin_responder), 'responded' (ya respondida, también: "
                "answered, replied, respondidas)."
            ),
        },
        "sentiment": {
            "type": "string",
            "default": "any",
            "description": (
                "Filtrá por sentimiento detectado. Acepta: 'any', "
                "'positive' (también: positivas), 'neutral' (también: "
                "mixed, neutrales), 'negative' (también: bad, "
                "negativas, malas). Reviews todavía no analizadas se "
                "excluyen cuando es distinto de 'any'."
            ),
        },
        "dish_name_contains": {
            "type": "string",
            "description": (
                "Substring (acento-insensible) que tiene que aparecer "
                "en el nombre del plato. Útil para 'reseñas de mi "
                "hamburguesa', 'qué dijeron del risotto'. Resuelve "
                "varios platos a la vez (no único)."
            ),
        },
        "min_rating": {
            "type": "number",
            "minimum": 1,
            "maximum": 5,
            "description": "Rating mínimo del cliente, escala 1-5.",
        },
        "max_rating": {
            "type": "number",
            "minimum": 1,
            "maximum": 5,
            "description": "Rating máximo del cliente, escala 1-5.",
        },
        "date_from": {
            "type": "string",
            "format": "date",
            "description": (
                "ISO date (YYYY-MM-DD). Filtra reseñas creadas a "
                "partir de esta fecha inclusive."
            ),
        },
        "date_to": {
            "type": "string",
            "format": "date",
            "description": (
                "ISO date (YYYY-MM-DD). Filtra reseñas creadas hasta "
                "esta fecha inclusive."
            ),
        },
        "sort": {
            "type": "string",
            "default": "recent",
            "description": (
                "Orden. Acepta: 'recent' (newest, latest, más "
                "reciente), 'oldest' (earliest, más vieja), "
                "'rating_high' (best, top_rated, mejores), "
                "'rating_low' (worst_rating), 'most_negative' "
                "(harshest, peores), 'most_positive' (happiest)."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "default": 10,
        },
    },
    "additionalProperties": False,
}


def _resolve_synonym(
    value: Any, synonyms: dict[str, str], default: str
) -> tuple[str, bool]:
    """Returns (canonical, was_recognised). Falls back to default when
    the input is missing or unrecognised — the tool always proceeds
    with a sane default so the LLM gets data, not an error."""
    if value is None:
        return default, True
    if not isinstance(value, str):
        return default, False
    key = _norm_key(value)
    if not key:
        return default, True
    canonical = synonyms.get(key)
    if canonical is None:
        return default, False
    return canonical, True


def make_list_reviews_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        notes: list[str] = []  # surface input issues without failing.

        responded_arg, ok = _resolve_synonym(
            args.get("responded_status"), _RESPONDED_SYNONYMS, "any"
        )
        if not ok:
            notes.append(
                f"responded_status={args.get('responded_status')!r} no "
                f"matchea ninguna opción; usé 'any'."
            )

        sentiment_canonical, ok = _resolve_synonym(
            args.get("sentiment"), _SENTIMENT_SYNONYMS, "any"
        )
        if not ok:
            notes.append(
                f"sentiment={args.get('sentiment')!r} no matchea; usé 'any'."
            )
        sentiment_filter = (
            SentimentLabel(sentiment_canonical)
            if sentiment_canonical != "any"
            else None
        )

        sort_arg, ok = _resolve_synonym(args.get("sort"), _SORT_SYNONYMS, "recent")
        if not ok:
            notes.append(
                f"sort={args.get('sort')!r} no matchea; usé 'recent'."
            )

        try:
            limit = max(1, min(50, int(args.get("limit", 10))))
        except (TypeError, ValueError):
            limit = 10
            notes.append("limit no era un entero; usé 10.")

        # Optional structured filters
        dish_name = args.get("dish_name_contains")
        min_rating = args.get("min_rating")
        max_rating = args.get("max_rating")
        date_from_raw = args.get("date_from")
        date_to_raw = args.get("date_to")

        from datetime import date

        date_from: date | None = None
        date_to: date | None = None
        if date_from_raw:
            try:
                date_from = date.fromisoformat(str(date_from_raw))
            except ValueError:
                notes.append(
                    f"date_from={date_from_raw!r} no es ISO YYYY-MM-DD; ignoré."
                )
        if date_to_raw:
            try:
                date_to = date.fromisoformat(str(date_to_raw))
            except ValueError:
                notes.append(
                    f"date_to={date_to_raw!r} no es ISO YYYY-MM-DD; ignoré."
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

        if responded_arg == "pending":
            stmt = stmt.where(DishReviewOwnerResponse.review_id.is_(None))
        elif responded_arg == "responded":
            stmt = stmt.where(DishReviewOwnerResponse.review_id.is_not(None))

        if sentiment_filter is not None:
            stmt = stmt.where(DishReview.sentiment_label == sentiment_filter)

        if dish_name and isinstance(dish_name, str) and dish_name.strip():
            # Pull all dish IDs in scope whose name matches (accent-insensitive).
            all_dishes = list(
                (
                    await db.execute(
                        select(Dish.id, Dish.name).where(
                            Dish.restaurant_id == restaurant_scope_id
                        )
                    )
                ).all()
            )
            needle = _normalize_for_search(dish_name)
            matching_ids = [
                row.id
                for row in all_dishes
                if needle in _normalize_for_search(row.name)
            ]
            if not matching_ids:
                return {
                    "count": 0,
                    "applied_filters": {
                        "responded_status": responded_arg,
                        "sentiment": sentiment_canonical,
                        "dish_name_contains": dish_name,
                        "sort": sort_arg,
                        "limit": limit,
                    },
                    "notes": notes
                    + [
                        f"Ningún plato del restaurante matchea "
                        f"'{dish_name}'. Mostrale al owner los platos "
                        "del menú (rank_my_dishes) y preguntale a "
                        "cuál se refería."
                    ],
                    "reviews": [],
                }
            stmt = stmt.where(DishReview.dish_id.in_(matching_ids))

        if min_rating is not None:
            try:
                stmt = stmt.where(DishReview.rating >= float(min_rating))
            except (TypeError, ValueError):
                notes.append(f"min_rating={min_rating!r} inválido; ignoré.")
        if max_rating is not None:
            try:
                stmt = stmt.where(DishReview.rating <= float(max_rating))
            except (TypeError, ValueError):
                notes.append(f"max_rating={max_rating!r} inválido; ignoré.")

        if date_from is not None:
            stmt = stmt.where(func.date(DishReview.created_at) >= date_from)
        if date_to is not None:
            stmt = stmt.where(func.date(DishReview.created_at) <= date_to)

        if sort_arg == "most_negative":
            stmt = stmt.order_by(
                asc(DishReview.sentiment_score).nullslast(),
                DishReview.created_at.desc(),
            )
        elif sort_arg == "most_positive":
            stmt = stmt.order_by(
                DishReview.sentiment_score.desc().nullslast(),
                DishReview.created_at.desc(),
            )
        elif sort_arg == "rating_high":
            stmt = stmt.order_by(
                DishReview.rating.desc(),
                DishReview.created_at.desc(),
            )
        elif sort_arg == "rating_low":
            stmt = stmt.order_by(
                DishReview.rating.asc(),
                DishReview.created_at.desc(),
            )
        elif sort_arg == "oldest":
            stmt = stmt.order_by(DishReview.created_at.asc())
        else:  # "recent"
            stmt = stmt.order_by(DishReview.created_at.desc())

        rows = list((await db.execute(stmt.limit(limit))).all())

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
        applied = {
            "responded_status": responded_arg,
            "sentiment": sentiment_canonical,
            "sort": sort_arg,
            "limit": limit,
        }
        if dish_name:
            applied["dish_name_contains"] = dish_name
        if min_rating is not None:
            applied["min_rating"] = min_rating
        if max_rating is not None:
            applied["max_rating"] = max_rating
        if date_from is not None:
            applied["date_from"] = date_from.isoformat()
        if date_to is not None:
            applied["date_to"] = date_to.isoformat()

        result: dict[str, Any] = {
            "restaurant_id": restaurant_scope_id,
            "count": len(items),
            "applied_filters": applied,
            "reviews": items,
        }
        if notes:
            result["notes"] = notes
        return result

    return ToolSpec(
        name="list_reviews",
        description=(
            "Single tool for ANY question about reviews of this "
            "restaurant. Compose filters: ``responded_status``, "
            "``sentiment``, ``dish_name_contains`` (substring "
            "accent-insensitive), ``min_rating``/``max_rating`` (1-5), "
            "``date_from``/``date_to`` (ISO YYYY-MM-DD). Choose order "
            "with ``sort`` (recent/oldest/rating_high/rating_low/"
            "most_negative/most_positive — many synonyms accepted). "
            "Examples: 'última review' → sort='recent', limit=1; "
            "'qué dijeron de mi hamburguesa' → "
            "dish_name_contains='hamburguesa'; 'reseñas duras de "
            "abril' → date_from='2026-04-01', date_to='2026-04-30', "
            "sort='most_negative'; 'top 3 mejores' → "
            "sort='rating_high', limit=3. The tool is **forgiving**: "
            "synonyms work, unrecognised inputs fall back to defaults "
            "and surface in ``notes``. The response always includes "
            "``applied_filters`` so you can see exactly what ran. "
            "Always pick the loosest filters the owner asked for — "
            "don't invent constraints."
        ),
        input_schema=LIST_REVIEWS_SCHEMA,
        handler=handler,
    )
