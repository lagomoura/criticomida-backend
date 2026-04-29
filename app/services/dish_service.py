from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Float, and_, case, cast, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import (
    Dish,
    DishReview,
    DishReviewImage,
    DishReviewProsCons,
    DishReviewProsConsType,
    DishReviewTag,
    PortionSize,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.services.image_cleanup import delete_images_for_dish
from app.services.rating_service import update_dish_rating


async def get_dishes_for_restaurant(
    db: AsyncSession, restaurant_id: uuid.UUID
) -> list[Dish]:
    """Get all dishes for a restaurant."""
    result = await db.execute(
        select(Dish)
        .where(Dish.restaurant_id == restaurant_id)
        .order_by(Dish.created_at.desc())
    )
    return list(result.scalars().all())


async def get_dish_detail(
    db: AsyncSession, dish_id: uuid.UUID
) -> Dish | None:
    """Get dish with all reviews eagerly loaded."""
    result = await db.execute(
        select(Dish)
        .options(
            selectinload(Dish.reviews).selectinload(DishReview.user),
            selectinload(Dish.reviews).selectinload(DishReview.pros_cons),
            selectinload(Dish.reviews).selectinload(DishReview.tags),
            selectinload(Dish.reviews).selectinload(DishReview.images),
        )
        .where(Dish.id == dish_id)
    )
    return result.scalar_one_or_none()


async def get_dish_by_id(
    db: AsyncSession, dish_id: uuid.UUID
) -> Dish | None:
    """Get a dish by id without eager loading."""
    result = await db.execute(select(Dish).where(Dish.id == dish_id))
    return result.scalar_one_or_none()


# ----- Aggregation queries (page-perfil endpoints) -----


async def is_signature_dish(
    db: AsyncSession, dish: Dish, *, top_n: int = 4
) -> bool:
    """True when dish is among the top-N reviewed dishes of its restaurant."""
    if dish.review_count <= 0:
        return False
    stmt = (
        select(Dish.id)
        .where(Dish.restaurant_id == dish.restaurant_id, Dish.review_count > 0)
        .order_by(desc(Dish.computed_rating), desc(Dish.review_count))
        .limit(top_n)
    )
    top_ids = {row for row in (await db.execute(stmt)).scalars().all()}
    return dish.id in top_ids


async def compute_dish_aggregates(
    db: AsyncSession, dish_id: uuid.UUID, *, top_limit: int = 12
) -> dict:
    """Aggregated taste profile for a dish — pros/cons, tags, distributions."""

    pros_cons_stmt = (
        select(
            DishReviewProsCons.type,
            DishReviewProsCons.text,
            func.count().label("cnt"),
        )
        .join(DishReview, DishReview.id == DishReviewProsCons.dish_review_id)
        .where(DishReview.dish_id == dish_id)
        .group_by(DishReviewProsCons.type, DishReviewProsCons.text)
        .order_by(desc("cnt"))
    )
    pros_cons_rows = (await db.execute(pros_cons_stmt)).all()

    pros_top: list[dict] = []
    cons_top: list[dict] = []
    for type_value, text_value, cnt in pros_cons_rows:
        bucket = pros_top if type_value == DishReviewProsConsType.pro else cons_top
        if len(bucket) < top_limit:
            bucket.append({"text": text_value, "count": cnt})

    tags_stmt = (
        select(DishReviewTag.tag, func.count().label("cnt"))
        .join(DishReview, DishReview.id == DishReviewTag.dish_review_id)
        .where(DishReview.dish_id == dish_id)
        .group_by(DishReviewTag.tag)
        .order_by(desc("cnt"))
        .limit(top_limit)
    )
    tags_rows = (await db.execute(tags_stmt)).all()
    tags_top = [{"tag": tag, "count": cnt} for tag, cnt in tags_rows]

    histogram_stmt = (
        select(
            func.floor(DishReview.rating).label("bucket"),
            func.count().label("cnt"),
        )
        .where(DishReview.dish_id == dish_id)
        .group_by("bucket")
    )
    histogram_rows = (await db.execute(histogram_stmt)).all()
    rating_histogram: dict[str, int] = {str(i): 0 for i in range(1, 6)}
    for bucket, cnt in histogram_rows:
        if bucket is None:
            continue
        key = str(int(bucket))
        if key in rating_histogram:
            rating_histogram[key] = cnt

    portion_stmt = (
        select(DishReview.portion_size, func.count().label("cnt"))
        .where(DishReview.dish_id == dish_id)
        .group_by(DishReview.portion_size)
    )
    portion_rows = (await db.execute(portion_stmt)).all()
    portion_distribution: dict[str, int] = {
        "small": 0,
        "medium": 0,
        "large": 0,
        "no_answer": 0,
    }
    for portion, cnt in portion_rows:
        if portion is None:
            portion_distribution["no_answer"] = cnt
        else:
            portion_distribution[portion.value] = cnt

    woa_stmt = select(
        func.count(case((DishReview.would_order_again.is_(True), 1))).label("yes"),
        func.count(case((DishReview.would_order_again.is_(False), 1))).label("no"),
        func.count(case((DishReview.would_order_again.is_(None), 1))).label("no_answer"),
        func.count().label("total"),
    ).where(DishReview.dish_id == dish_id)
    woa_row = (await db.execute(woa_stmt)).one()
    answered = (woa_row.yes or 0) + (woa_row.no or 0)
    woa_pct: float | None = (
        round(100.0 * woa_row.yes / answered, 1) if answered > 0 else None
    )

    # Pilares técnicos (presentation, value_prop, execution): cada uno
    # nullable 1..3. Devuelvo counts por valor + total respondidos + avg.
    pillars_stmt = select(
        func.count(case((DishReview.presentation == 1, 1))).label("pres_1"),
        func.count(case((DishReview.presentation == 2, 1))).label("pres_2"),
        func.count(case((DishReview.presentation == 3, 1))).label("pres_3"),
        func.avg(DishReview.presentation).label("pres_avg"),
        func.count(case((DishReview.value_prop == 1, 1))).label("val_1"),
        func.count(case((DishReview.value_prop == 2, 1))).label("val_2"),
        func.count(case((DishReview.value_prop == 3, 1))).label("val_3"),
        func.avg(DishReview.value_prop).label("val_avg"),
        func.count(case((DishReview.execution == 1, 1))).label("exec_1"),
        func.count(case((DishReview.execution == 2, 1))).label("exec_2"),
        func.count(case((DishReview.execution == 3, 1))).label("exec_3"),
        func.avg(DishReview.execution).label("exec_avg"),
    ).where(DishReview.dish_id == dish_id)
    pillars_row = (await db.execute(pillars_stmt)).one()

    def _pillar_payload(c1: int, c2: int, c3: int, avg) -> dict:
        answered = (c1 or 0) + (c2 or 0) + (c3 or 0)
        return {
            "one": c1 or 0,
            "two": c2 or 0,
            "three": c3 or 0,
            "answered": answered,
            "avg": float(round(float(avg), 2)) if avg is not None else None,
        }

    pillars = {
        "presentation": _pillar_payload(
            pillars_row.pres_1, pillars_row.pres_2, pillars_row.pres_3, pillars_row.pres_avg
        ),
        "value_prop": _pillar_payload(
            pillars_row.val_1, pillars_row.val_2, pillars_row.val_3, pillars_row.val_avg
        ),
        "execution": _pillar_payload(
            pillars_row.exec_1, pillars_row.exec_2, pillars_row.exec_3, pillars_row.exec_avg
        ),
    }

    photos_count_stmt = (
        select(func.count(DishReviewImage.id))
        .join(DishReview, DishReview.id == DishReviewImage.dish_review_id)
        .where(DishReview.dish_id == dish_id)
    )
    photos_count = (await db.execute(photos_count_stmt)).scalar_one()

    unique_eaters_stmt = select(func.count(func.distinct(DishReview.user_id))).where(
        DishReview.dish_id == dish_id
    )
    unique_eaters = (await db.execute(unique_eaters_stmt)).scalar_one()

    return {
        "pros_top": pros_top,
        "cons_top": cons_top,
        "tags_top": tags_top,
        "rating_histogram": rating_histogram,
        "portion_distribution": portion_distribution,
        "would_order_again": {
            "yes": woa_row.yes or 0,
            "no": woa_row.no or 0,
            "no_answer": woa_row.no_answer or 0,
            "pct": woa_pct,
        },
        "pillars": pillars,
        "photos_count": photos_count or 0,
        "unique_eaters": unique_eaters or 0,
    }


async def get_dish_photos(
    db: AsyncSession,
    dish_id: uuid.UUID,
    *,
    limit: int = 24,
    cursor: str | None = None,  # noqa: ARG001 — kept for signature parity
) -> dict:
    """Photos for a dish — UGC review uploads ordered by uploaded_at desc.

    Includes the dish.cover_image_url as a synthetic first item if present and
    not already covered by a UGC photo with the same URL.
    """

    ugc_stmt = (
        select(
            DishReviewImage.id.label("photo_id"),
            DishReviewImage.url,
            DishReviewImage.alt_text,
            DishReviewImage.uploaded_at,
            DishReview.id.label("review_id"),
            User.id.label("user_id"),
            User.handle,
            User.display_name,
            DishReview.is_anonymous,
        )
        .join(DishReview, DishReview.id == DishReviewImage.dish_review_id)
        .join(User, User.id == DishReview.user_id)
        .where(DishReview.dish_id == dish_id)
        .order_by(DishReviewImage.uploaded_at.desc())
        .limit(limit + 1)
    )
    ugc_rows = (await db.execute(ugc_stmt)).all()

    items: list[dict] = []
    seen_urls: set[str] = set()

    dish_row = (
        await db.execute(
            select(Dish.cover_image_url, Dish.name, Dish.created_at, Dish.created_by)
            .where(Dish.id == dish_id)
        )
    ).first()
    if dish_row and dish_row.cover_image_url:
        seen_urls.add(dish_row.cover_image_url)
        items.append(
            {
                "id": str(dish_id),
                "url": dish_row.cover_image_url,
                "alt_text": dish_row.name,
                "taken_at": dish_row.created_at,
                "dish_id": dish_id,
                "dish_name": dish_row.name,
                "review_id": None,
                "user_id": dish_row.created_by,
                "user_handle": None,
                "user_display_name": None,
                "is_cover": True,
            }
        )

    for row in ugc_rows:
        if len(items) >= limit:
            break
        if row.url in seen_urls:
            continue
        seen_urls.add(row.url)
        items.append(
            {
                "id": str(row.photo_id),
                "url": row.url,
                "alt_text": row.alt_text,
                "taken_at": row.uploaded_at,
                "dish_id": dish_id,
                "dish_name": dish_row.name if dish_row else None,
                "review_id": row.review_id,
                "user_id": row.user_id,
                "user_handle": None if row.is_anonymous else row.handle,
                "user_display_name": "Anónimo" if row.is_anonymous else row.display_name,
                "is_cover": False,
            }
        )

    return {"items": items, "next_cursor": None}


async def get_dish_diary_stats(
    db: AsyncSession, dish_id: uuid.UUID
) -> dict:
    """Diary-style stats for a dish: unique eaters, totals, recent eaters."""

    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    counts_stmt = select(
        func.count(DishReview.id).label("total"),
        func.count(func.distinct(DishReview.user_id)).label("unique"),
        func.count(case((DishReview.created_at >= seven_days_ago, 1))).label("last_7d"),
    ).where(DishReview.dish_id == dish_id)
    counts_row = (await db.execute(counts_stmt)).one()

    recent_stmt = (
        select(
            User.id,
            User.handle,
            User.display_name,
            User.avatar_url,
            DishReview.is_anonymous,
            func.max(DishReview.created_at).label("last_at"),
        )
        .join(DishReview, DishReview.user_id == User.id)
        .where(DishReview.dish_id == dish_id)
        .group_by(
            User.id,
            User.handle,
            User.display_name,
            User.avatar_url,
            DishReview.is_anonymous,
        )
        .order_by(desc("last_at"))
        .limit(8)
    )
    recent_rows = (await db.execute(recent_stmt)).all()
    recent_eaters = [
        {
            "id": row.id,
            "handle": None if row.is_anonymous else row.handle,
            "display_name": "Anónimo" if row.is_anonymous else row.display_name,
            "avatar_url": None if row.is_anonymous else row.avatar_url,
        }
        for row in recent_rows
    ]

    return {
        "unique_eaters": counts_row.unique or 0,
        "reviews_total": counts_row.total or 0,
        "reviews_last_7d": counts_row.last_7d or 0,
        "recent_eaters": recent_eaters,
    }


_STOPWORDS_ES = {
    "de", "la", "el", "los", "las", "un", "una", "y", "o", "a",
    "al", "en", "con", "del", "por", "para", "su", "sus", "es",
    "que", "se", "lo", "le", "les", "mi", "tu",
}


def _tokenize_dish_name(name: str) -> list[str]:
    """Split dish name into searchable tokens (>3 chars, no stopwords)."""
    raw = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ]+", name.lower())
    return [t for t in raw if len(t) > 3 and t not in _STOPWORDS_ES]


async def get_related_dishes(
    db: AsyncSession,
    dish: Dish,
    *,
    limit: int = 6,
) -> list[dict]:
    """Other dishes with similar names at other restaurants.

    Token-level ILIKE match (no fuzzy/trigram). Prefers same-city when known.
    """
    tokens = _tokenize_dish_name(dish.name)
    if not tokens:
        return []

    name_filters = [Dish.name.ilike(f"%{token}%") for token in tokens]

    stmt = (
        select(
            Dish.id,
            Dish.name,
            Dish.cover_image_url,
            Dish.computed_rating,
            Dish.review_count,
            Dish.price_tier,
            Restaurant.id.label("restaurant_id"),
            Restaurant.slug.label("restaurant_slug"),
            Restaurant.name.label("restaurant_name"),
            Restaurant.location_name.label("restaurant_location"),
            Restaurant.city.label("restaurant_city"),
        )
        .join(Restaurant, Restaurant.id == Dish.restaurant_id)
        .where(
            Dish.restaurant_id != dish.restaurant_id,
            or_(*name_filters),
        )
        .order_by(desc(Dish.review_count), desc(Dish.computed_rating))
        .limit(limit * 3)
    )
    rows = (await db.execute(stmt)).all()

    # Resolve home restaurant city for prioritising matches in same city.
    home_city_row = (
        await db.execute(
            select(Restaurant.city).where(Restaurant.id == dish.restaurant_id)
        )
    ).first()
    home_city = home_city_row.city if home_city_row else None

    same_city: list[dict] = []
    other_city: list[dict] = []
    for row in rows:
        item = {
            "id": row.id,
            "name": row.name,
            "cover_image_url": row.cover_image_url,
            "computed_rating": row.computed_rating,
            "review_count": row.review_count,
            "price_tier": row.price_tier.value if row.price_tier else None,
            "restaurant_id": row.restaurant_id,
            "restaurant_slug": row.restaurant_slug,
            "restaurant_name": row.restaurant_name,
            "restaurant_location": row.restaurant_location,
            "restaurant_city": row.restaurant_city,
        }
        if home_city and row.restaurant_city == home_city:
            same_city.append(item)
        else:
            other_city.append(item)

    return (same_city + other_city)[:limit]


async def merge_dishes(
    db: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> dict:
    """Merge `source_id` dish into `target_id`. Atomic.

    Both dishes must belong to the same restaurant — cross-restaurant dish
    merges go through `merge_restaurants` instead. Moves all `dish_reviews`
    from source to target, optionally inherits the cover image when target
    has none, deletes the source's polymorphic dish_cover image rows + files,
    deletes the source row, and recomputes the target's rating aggregate.

    Caller must commit. Raises:
      - ValueError if source_id == target_id
      - ValueError if source/target belong to different restaurants
      - LookupError if either dish doesn't exist
    """
    if source_id == target_id:
        raise ValueError("source and target must differ")

    # Lock both rows up-front; order by uuid to avoid deadlocks if two merges
    # race against each other.
    first, second = sorted([source_id, target_id], key=str)
    rows = (
        await db.execute(
            text(
                "SELECT id, restaurant_id, name, cover_image_url "
                "FROM dishes WHERE id IN (:a, :b) FOR UPDATE"
            ),
            {"a": first, "b": second},
        )
    ).all()
    by_id = {row.id: row for row in rows}
    if source_id not in by_id:
        raise LookupError("source dish not found")
    if target_id not in by_id:
        raise LookupError("target dish not found")

    source_row = by_id[source_id]
    target_row = by_id[target_id]

    if source_row.restaurant_id != target_row.restaurant_id:
        raise ValueError(
            "source and target dishes must belong to the same restaurant"
        )

    summary: dict = {
        "source_id": source_id,
        "target_id": target_id,
        "source_name": source_row.name,
        "target_name": target_row.name,
        "cover_inherited": False,
    }

    # 1) Inherit cover_image_url when target has none — keeps the merge from
    #    accidentally erasing the only photo of the dish.
    if target_row.cover_image_url is None and source_row.cover_image_url:
        await db.execute(
            text(
                "UPDATE dishes SET cover_image_url = :url WHERE id = :t"
            ),
            {"url": source_row.cover_image_url, "t": target_id},
        )
        summary["cover_inherited"] = True

    # 2) Move dish_reviews. There's no UNIQUE constraint on (dish_id, user_id)
    #    since migration 017, so a plain bulk UPDATE is safe — duplicates
    #    across users or even within one user become valid timeline entries.
    moved_reviews = await db.execute(
        text("UPDATE dish_reviews SET dish_id = :t WHERE dish_id = :s"),
        {"s": source_id, "t": target_id},
    )
    summary["reviews_moved"] = moved_reviews.rowcount

    # 3) Delete source's polymorphic dish_cover images (table + files on disk).
    #    Target keeps its own. The cover URL we may have inherited above is just
    #    a string column on the dish row; the image rows were tied to source_id.
    await delete_images_for_dish(db, source_id)

    # 4) Drop the source row. dish_reviews already moved; the cascade kills any
    #    leftover children (none expected).
    await db.execute(
        text("DELETE FROM dishes WHERE id = :s"), {"s": source_id}
    )

    # 5) Recompute target rating from the new review set.
    await db.flush()
    await update_dish_rating(db, target_id)

    return summary
