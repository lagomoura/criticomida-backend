"""Aggregates a user's review history into a structured taste profile.

The chatbot injects this snapshot into its system prompt so it can greet
by name, reason about preferences, and respect dietary restrictions.

Heuristics (every recompute runs them all on the user's full history):

- ``dominant_pillar``: argmax over avg(presentation), avg(execution),
  avg(value_prop) across the user's reviews. None if the user has rated
  fewer than 3 reviews on any pillar.
- ``top_neighborhoods``: top 3 distinct ``location_name`` substrings
  (we keep them as-is — they're often "Palermo, Buenos Aires").
- ``top_categories``: top 3 category slugs by review count.
- ``avg_price_band``: bucketed mean of the dishes' price_tier
  (low=$, mid=$$, high=$$$).
- ``favorite_tags``: top 5 dish_review_tags by count.
- ``preferred_hours``: top 3 hours of day from ``time_tasted`` when
  present (falls back to created_at hour).

Allergies are NOT touched here: only ``update_taste_profile`` (the chat
tool) writes them, because they require an explicit declaration.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chat import (
    PriceBand,
    TastePillar,
    UserTasteProfile,
)
from app.models.dish import (
    Dish,
    DishReview,
    PriceTier,
)
from app.models.restaurant import Restaurant

logger = logging.getLogger(__name__)


# Recompute when the user has added this many reviews since the last
# refresh (avoids running the heavy aggregate query on every write).
RECOMPUTE_REVIEW_DELTA = 1


def _bucket_price_band(avg: float | None) -> PriceBand | None:
    if avg is None:
        return None
    if avg < 1.5:
        return PriceBand.low
    if avg < 2.5:
        return PriceBand.mid
    return PriceBand.high


def _price_tier_rank(tier: PriceTier | None) -> int | None:
    if tier is None:
        return None
    return {PriceTier.low: 1, PriceTier.mid: 2, PriceTier.high: 3}[tier]


async def _compute_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> dict[str, object]:
    # All reviews by the user, joined to dish + restaurant + category.
    stmt = (
        select(DishReview)
        .where(DishReview.user_id == user_id)
        .options(
            selectinload(DishReview.dish)
            .selectinload(Dish.restaurant)
            .selectinload(Restaurant.category),
            selectinload(DishReview.tags),
        )
    )
    reviews = list((await db.execute(stmt)).scalars().unique().all())
    review_count = len(reviews)

    if review_count == 0:
        return {
            "dominant_pillar": None,
            "top_neighborhoods": [],
            "top_categories": [],
            "avg_price_band": None,
            "favorite_tags": [],
            "preferred_hours": [],
            "review_count_at_last_compute": 0,
        }

    pillar_sums: dict[TastePillar, list[int]] = {
        TastePillar.presentation: [],
        TastePillar.execution: [],
        TastePillar.value_prop: [],
    }
    locations: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    hours: Counter[int] = Counter()
    price_ranks: list[int] = []

    for r in reviews:
        if r.presentation is not None:
            pillar_sums[TastePillar.presentation].append(int(r.presentation))
        if r.execution is not None:
            pillar_sums[TastePillar.execution].append(int(r.execution))
        if r.value_prop is not None:
            pillar_sums[TastePillar.value_prop].append(int(r.value_prop))

        if r.dish and r.dish.restaurant:
            rest = r.dish.restaurant
            if rest.location_name:
                locations[rest.location_name] += 1
            if rest.category and rest.category.slug:
                categories[rest.category.slug] += 1

        for t in r.tags or []:
            if t.tag:
                tags[t.tag] += 1

        if r.time_tasted is not None:
            hours[r.time_tasted.hour] += 1
        elif r.created_at is not None:
            hours[r.created_at.hour] += 1

        if r.dish and r.dish.price_tier:
            rank = _price_tier_rank(r.dish.price_tier)
            if rank is not None:
                price_ranks.append(rank)

    # Pick the dominant pillar only when we have at least 3 ratings on it
    # (smaller samples are noise).
    pillar_avgs: dict[TastePillar, float] = {}
    for pillar, vals in pillar_sums.items():
        if len(vals) >= 3:
            pillar_avgs[pillar] = sum(vals) / len(vals)
    dominant: TastePillar | None = None
    if pillar_avgs:
        dominant = max(pillar_avgs.items(), key=lambda kv: kv[1])[0]

    avg_price = (
        sum(price_ranks) / len(price_ranks) if price_ranks else None
    )

    return {
        "dominant_pillar": dominant,
        "top_neighborhoods": [n for n, _ in locations.most_common(3)],
        "top_categories": [c for c, _ in categories.most_common(3)],
        "avg_price_band": _bucket_price_band(avg_price),
        "favorite_tags": [t for t, _ in tags.most_common(5)],
        "preferred_hours": [h for h, _ in hours.most_common(3)],
        "review_count_at_last_compute": review_count,
    }


async def recompute_taste_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> UserTasteProfile:
    """Recompute and upsert the profile for ``user_id``.

    Preserves the user-declared ``allergies`` field — we never overwrite
    it from inferred data.
    """
    snapshot = await _compute_profile(db, user_id)

    # Read existing row (if any) to preserve the allergy list.
    existing = (
        await db.execute(
            select(UserTasteProfile).where(
                UserTasteProfile.user_id == user_id
            )
        )
    ).scalars().first()
    allergies = existing.allergies if existing else []
    version = existing.version if existing else 1

    payload = {
        "user_id": user_id,
        "dominant_pillar": snapshot["dominant_pillar"],
        "top_neighborhoods": snapshot["top_neighborhoods"],
        "top_categories": snapshot["top_categories"],
        "avg_price_band": snapshot["avg_price_band"],
        "favorite_tags": snapshot["favorite_tags"],
        "preferred_hours": snapshot["preferred_hours"],
        "allergies": allergies,
        "version": version,
        "review_count_at_last_compute": snapshot[
            "review_count_at_last_compute"
        ],
        "updated_at": datetime.now(timezone.utc),
    }

    upsert = (
        insert(UserTasteProfile)
        .values(**payload)
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                k: v
                for k, v in payload.items()
                if k not in ("user_id", "allergies")
            },
        )
    )
    await db.execute(upsert)

    refreshed = (
        await db.execute(
            select(UserTasteProfile).where(
                UserTasteProfile.user_id == user_id
            )
        )
    ).scalars().first()
    assert refreshed is not None  # Just upserted.
    return refreshed


async def get_taste_profile(
    db: AsyncSession, user_id: uuid.UUID
) -> UserTasteProfile | None:
    """Read-only fetch. Returns ``None`` if the user has never been
    profiled — caller decides whether to compute on the fly."""
    return (
        await db.execute(
            select(UserTasteProfile).where(
                UserTasteProfile.user_id == user_id
            )
        )
    ).scalars().first()


async def maybe_refresh_after_review(
    db: AsyncSession, user_id: uuid.UUID
) -> None:
    """Hook called after a user writes/edits a review.

    Recomputes the profile when the review count has grown by at least
    ``RECOMPUTE_REVIEW_DELTA`` since the last snapshot, otherwise no-op.
    Errors are logged and swallowed so the review write succeeds even if
    profile aggregation breaks.
    """
    try:
        existing = await get_taste_profile(db, user_id)
        current = (
            await db.execute(
                select(func.count())
                .select_from(DishReview)
                .where(DishReview.user_id == user_id)
            )
        ).scalar_one()

        last = existing.review_count_at_last_compute if existing else 0
        if current - last < RECOMPUTE_REVIEW_DELTA and existing is not None:
            return
        await recompute_taste_profile(db, user_id)
    except Exception:
        logger.exception("maybe_refresh_after_review failed")
