import base64
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Float, case, cast, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.dish import Dish, DishReview, DishReviewImage
from app.models.image import Image
from app.models.menu import Menu
from app.models.restaurant import (
    ProsConsType,
    Restaurant,
    RestaurantProsCons,
    RestaurantRatingDimension,
    VisitDiaryEntry,
)
from app.models.user import User
from app.services.rating_service import update_dish_rating, update_restaurant_rating


async def get_restaurant_list(
    db: AsyncSession,
    *,
    category_slug: str | None = None,
    search: str | None = None,
    min_rating: Decimal | None = None,
    max_rating: Decimal | None = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Restaurant], int]:
    """Return paginated list of restaurants with filters applied."""
    stmt = select(Restaurant).options(selectinload(Restaurant.category))

    if category_slug:
        stmt = stmt.join(Category).where(Category.slug == category_slug)

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            Restaurant.name.ilike(pattern) | Restaurant.location_name.ilike(pattern)
        )

    if min_rating is not None:
        stmt = stmt.where(Restaurant.computed_rating >= min_rating)

    if max_rating is not None:
        stmt = stmt.where(Restaurant.computed_rating <= max_rating)

    # Count total before pagination
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    # Apply pagination
    offset = (page - 1) * per_page
    stmt = stmt.order_by(Restaurant.created_at.desc()).offset(offset).limit(per_page)

    result = await db.execute(stmt)
    restaurants = list(result.scalars().all())

    return restaurants, total


def _try_uuid(value: str) -> uuid.UUID | None:
    """Parse `value` as UUID; return None if not a valid UUID string."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


async def get_restaurant_detail(
    db: AsyncSession, slug_or_id: str
) -> Restaurant | None:
    """Return full restaurant detail with all relationships loaded.

    Accepts either a slug or a UUID string — UUIDs are tried as id, then slug
    is also tried as fallback (some legacy slugs happen to be UUID-shaped).
    """
    options = [
        selectinload(Restaurant.category),
        selectinload(Restaurant.creator),
        selectinload(Restaurant.dishes).selectinload(Dish.reviews),
        selectinload(Restaurant.dimension_ratings),
        selectinload(Restaurant.pros_cons),
        selectinload(Restaurant.diary_entries),
        selectinload(Restaurant.menu),
    ]
    parsed = _try_uuid(slug_or_id)
    if parsed is not None:
        result = await db.execute(
            select(Restaurant).options(*options).where(Restaurant.id == parsed)
        )
        found = result.scalar_one_or_none()
        if found is not None:
            return found
    result = await db.execute(
        select(Restaurant).options(*options).where(Restaurant.slug == slug_or_id)
    )
    return result.scalar_one_or_none()


async def get_restaurant_by_slug(
    db: AsyncSession, slug_or_id: str
) -> Restaurant | None:
    """Get a restaurant without eager loading. Accepts slug or UUID."""
    parsed = _try_uuid(slug_or_id)
    if parsed is not None:
        result = await db.execute(
            select(Restaurant).where(Restaurant.id == parsed)
        )
        found = result.scalar_one_or_none()
        if found is not None:
            return found
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == slug_or_id)
    )
    return result.scalar_one_or_none()


# Phase 2.2 thresholds. A candidate must satisfy BOTH to be returned.
# Tuned conservatively to avoid false positives in the "did you mean..." UX.
MATCH_NAME_SIMILARITY_THRESHOLD = 0.5
MATCH_DISTANCE_METERS_THRESHOLD = 50.0


async def find_match_candidates(
    db: AsyncSession,
    *,
    name: str,
    latitude: Decimal | float,
    longitude: Decimal | float,
    exclude_place_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Return restaurants likely to be duplicates of the one the user is about
    to create. A candidate must clear both `MATCH_NAME_SIMILARITY_THRESHOLD`
    (pg_trgm) and `MATCH_DISTANCE_METERS_THRESHOLD` (Haversine).

    `exclude_place_id` is the Google `place_id` the user just selected; we
    skip any row already keyed to it because that case is already covered
    by `find_restaurant_by_place_id` (Fase 2.1 dedup).
    """
    lat_param = float(latitude)
    lng_param = float(longitude)

    delta_lat = func.radians(cast(Restaurant.latitude, Float) - lat_param) / 2.0
    delta_lng = func.radians(cast(Restaurant.longitude, Float) - lng_param) / 2.0
    a = (
        func.power(func.sin(delta_lat), 2)
        + func.cos(func.radians(lat_param))
        * func.cos(func.radians(cast(Restaurant.latitude, Float)))
        * func.power(func.sin(delta_lng), 2)
    )
    distance_m_expr = 6371000.0 * 2.0 * func.asin(func.sqrt(a))
    # unaccent() so "Güerrín" matches "Guerrin" — the most common Spanish
    # accent variation in Google Places listings.
    name_sim_expr = func.similarity(
        func.unaccent(Restaurant.name), func.unaccent(name)
    )
    # Combined score: averages name similarity (0-1, higher is better) with the
    # complement of normalized distance (0-1, higher is closer). Used only to
    # rank candidates — both thresholds gate inclusion separately.
    confidence_expr = (
        name_sim_expr
        + (1.0 - distance_m_expr / MATCH_DISTANCE_METERS_THRESHOLD)
    ) / 2.0

    stmt = (
        select(
            Restaurant,
            name_sim_expr.label("name_sim"),
            distance_m_expr.label("distance_m"),
            confidence_expr.label("confidence"),
        )
        .where(
            Restaurant.latitude.is_not(None),
            Restaurant.longitude.is_not(None),
            name_sim_expr >= MATCH_NAME_SIMILARITY_THRESHOLD,
            distance_m_expr <= MATCH_DISTANCE_METERS_THRESHOLD,
        )
        .order_by(confidence_expr.desc())
        .limit(limit)
    )

    if exclude_place_id is not None:
        stmt = stmt.where(
            (Restaurant.google_place_id.is_(None))
            | (Restaurant.google_place_id != exclude_place_id)
        )

    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": r.id,
            "slug": r.slug,
            "name": r.name,
            "location_name": r.location_name,
            "latitude": r.latitude,
            "longitude": r.longitude,
            "google_place_id": r.google_place_id,
            "cover_image_url": r.cover_image_url,
            "computed_rating": r.computed_rating,
            "review_count": r.review_count,
            "name_similarity": float(name_sim),
            "distance_m": float(distance_m),
            "confidence_score": float(confidence),
        }
        for r, name_sim, distance_m, confidence in rows
    ]


async def find_restaurant_by_redirect(
    db: AsyncSession, slug: str
) -> uuid.UUID | None:
    """Lookup a restaurant_id from a previously-merged slug.

    `restaurant_slug_redirects` is populated by `merge_restaurants`. Used by
    GET /api/restaurants/{slug} as a fallback so old links keep working after
    an admin merges two duplicate restaurants.
    """
    result = await db.execute(
        text(
            "SELECT restaurant_id FROM restaurant_slug_redirects "
            "WHERE old_slug = :slug"
        ),
        {"slug": slug},
    )
    row = result.scalar_one_or_none()
    return row


async def merge_restaurants(
    db: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> dict:
    """Merge `source_id` restaurant into `target_id`. Atomic.

    Moves every FK that points at the source to the target, deletes the source
    row, and inserts a redirect from the source's slug. Conflicts on UNIQUE
    constraints (rating dimensions, dish names, menu) are resolved by keeping
    the target's row and dropping the source's. Aggregates on the target
    (dishes' computed_rating, restaurant's computed_rating + review_count) are
    recomputed from the new state.

    Caller must commit. Raises:
      - ValueError if source_id == target_id
      - LookupError if either restaurant doesn't exist
    """
    if source_id == target_id:
        raise ValueError("source and target must differ")

    # Lock both rows up-front so a concurrent admin can't half-merge the same
    # source twice. Order by uuid to avoid deadlocks if two merges race.
    first, second = sorted([source_id, target_id], key=str)
    rows = (
        await db.execute(
            text(
                "SELECT id, slug FROM restaurants "
                "WHERE id IN (:a, :b) FOR UPDATE"
            ),
            {"a": first, "b": second},
        )
    ).all()
    found = {row.id: row.slug for row in rows}
    if source_id not in found:
        raise LookupError("source restaurant not found")
    if target_id not in found:
        raise LookupError("target restaurant not found")

    source_slug = found[source_id]
    summary: dict = {"source_slug": source_slug}

    # 1) Dishes: handle name_normalized conflicts before bulk-moving.
    # 1a) Move reviews from source-side dishes that collide with target dishes.
    conflict_rows = (
        await db.execute(
            text(
                """
                WITH conflicts AS (
                    SELECT s.id AS source_dish_id, t.id AS target_dish_id
                    FROM dishes s
                    JOIN dishes t ON s.name_normalized = t.name_normalized
                    WHERE s.restaurant_id = :source AND t.restaurant_id = :target
                )
                UPDATE dish_reviews
                SET dish_id = c.target_dish_id
                FROM conflicts c
                WHERE dish_reviews.dish_id = c.source_dish_id
                RETURNING c.target_dish_id
                """
            ),
            {"source": source_id, "target": target_id},
        )
    ).all()
    target_dish_ids_with_moved_reviews = {row.target_dish_id for row in conflict_rows}
    summary["reviews_remapped"] = len(conflict_rows)

    # 1b) Delete the now-empty conflicting source dishes.
    deleted_dishes = await db.execute(
        text(
            """
            DELETE FROM dishes
            WHERE restaurant_id = :source
              AND name_normalized IN (
                SELECT name_normalized FROM dishes WHERE restaurant_id = :target
              )
            """
        ),
        {"source": source_id, "target": target_id},
    )
    summary["dishes_merged_into_target"] = deleted_dishes.rowcount

    # 1c) Move remaining (non-conflicting) source dishes to target.
    moved_dishes = await db.execute(
        text(
            "UPDATE dishes SET restaurant_id = :target "
            "WHERE restaurant_id = :source"
        ),
        {"source": source_id, "target": target_id},
    )
    summary["dishes_moved"] = moved_dishes.rowcount

    # 2) Menu — UNIQUE on restaurant_id; target wins.
    target_has_menu = (
        await db.execute(
            text("SELECT 1 FROM menus WHERE restaurant_id = :t"),
            {"t": target_id},
        )
    ).scalar_one_or_none() is not None
    if target_has_menu:
        deleted_menu = await db.execute(
            text("DELETE FROM menus WHERE restaurant_id = :s"),
            {"s": source_id},
        )
        summary["source_menu_deleted"] = deleted_menu.rowcount
    else:
        moved_menu = await db.execute(
            text(
                "UPDATE menus SET restaurant_id = :t WHERE restaurant_id = :s"
            ),
            {"s": source_id, "t": target_id},
        )
        summary["menu_moved"] = moved_menu.rowcount

    # 3) Pros/cons — no UNIQUE constraint, just move.
    moved_pc = await db.execute(
        text(
            "UPDATE restaurant_pros_cons SET restaurant_id = :t "
            "WHERE restaurant_id = :s"
        ),
        {"s": source_id, "t": target_id},
    )
    summary["pros_cons_moved"] = moved_pc.rowcount

    # 4) Rating dimensions — UNIQUE(restaurant, user, dimension). Target wins.
    dropped_dims = await db.execute(
        text(
            """
            DELETE FROM restaurant_rating_dimensions
            WHERE restaurant_id = :s
              AND (user_id, dimension) IN (
                SELECT user_id, dimension
                FROM restaurant_rating_dimensions
                WHERE restaurant_id = :t
              )
            """
        ),
        {"s": source_id, "t": target_id},
    )
    summary["rating_dimensions_dropped"] = dropped_dims.rowcount

    moved_dims = await db.execute(
        text(
            "UPDATE restaurant_rating_dimensions SET restaurant_id = :t "
            "WHERE restaurant_id = :s"
        ),
        {"s": source_id, "t": target_id},
    )
    summary["rating_dimensions_moved"] = moved_dims.rowcount

    # 5) Visit diary entries — no UNIQUE.
    moved_diary = await db.execute(
        text(
            "UPDATE visit_diary_entries SET restaurant_id = :t "
            "WHERE restaurant_id = :s"
        ),
        {"s": source_id, "t": target_id},
    )
    summary["diary_entries_moved"] = moved_diary.rowcount

    # 6) Images (polymorphic ref, not a FK).
    moved_imgs = await db.execute(
        text(
            "UPDATE images SET entity_id = :t "
            "WHERE entity_id = :s "
            "  AND entity_type IN ('restaurant_cover', 'restaurant_gallery')"
        ),
        {"s": source_id, "t": target_id},
    )
    summary["images_moved"] = moved_imgs.rowcount

    # 7) Existing redirects pointing AT source → re-point to target (chained merges).
    repointed = await db.execute(
        text(
            "UPDATE restaurant_slug_redirects SET restaurant_id = :t "
            "WHERE restaurant_id = :s"
        ),
        {"s": source_id, "t": target_id},
    )
    summary["redirects_repointed"] = repointed.rowcount

    # 8) Insert source's slug as a redirect to target. Upsert in case the slug
    #    was already in the redirect table from a prior merge (shouldn't happen
    #    since slugs are unique on `restaurants`, but defensive).
    await db.execute(
        text(
            "INSERT INTO restaurant_slug_redirects (old_slug, restaurant_id) "
            "VALUES (:slug, :t) "
            "ON CONFLICT (old_slug) DO UPDATE SET restaurant_id = EXCLUDED.restaurant_id"
        ),
        {"slug": source_slug, "t": target_id},
    )

    # 9) DELETE the source row. Must happen BEFORE recomputing target rating —
    #    otherwise CASCADE would zap the rating_dimensions/dishes we just moved
    #    to target if they share rows. Wait — they don't share rows; we updated
    #    them. CASCADE only kills rows still pointing at source (none left).
    await db.execute(
        text("DELETE FROM restaurants WHERE id = :s"),
        {"s": source_id},
    )

    # 10) Recompute aggregates after all moves are flushed to the session.
    await db.flush()
    for dish_id in target_dish_ids_with_moved_reviews:
        await update_dish_rating(db, dish_id)
    await update_restaurant_rating(db, target_id)

    return summary


async def find_restaurant_by_place_id(
    db: AsyncSession,
    place_id: str,
    *,
    eager: bool = False,
) -> Restaurant | None:
    """Lookup a restaurant by its Google Places id.

    Used both as a pre-check before INSERT (to dedupe Google selections that map
    to an existing entity) and as the recovery path when the unique-index
    `uq_restaurants_google_place_id` rejects a concurrent INSERT.

    Pass `eager=True` when the caller will return the row through
    `RestaurantResponse`, which requires `category` and `creator` to be loaded.
    """
    stmt = select(Restaurant).where(Restaurant.google_place_id == place_id)
    if eager:
        stmt = stmt.options(
            selectinload(Restaurant.category),
            selectinload(Restaurant.creator),
        )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_restaurant_gallery_images(
    db: AsyncSession, restaurant_id: uuid.UUID
) -> list[Image]:
    """Get gallery images for a restaurant."""
    from app.models.image import EntityType

    result = await db.execute(
        select(Image)
        .where(
            Image.entity_id == restaurant_id,
            Image.entity_type == EntityType.restaurant_gallery,
        )
        .order_by(Image.display_order)
    )
    return list(result.scalars().all())


# ----- Aggregation queries (page-perfil endpoints) -----


async def get_restaurant_aggregates(
    db: AsyncSession, restaurant_id: uuid.UUID, *, top_limit: int = 8
) -> dict:
    """Aggregated pros/cons frequency, dimension averages, photo & dish counts."""

    pros_cons_stmt = (
        select(
            RestaurantProsCons.type,
            RestaurantProsCons.text,
            func.count().label("cnt"),
        )
        .where(RestaurantProsCons.restaurant_id == restaurant_id)
        .group_by(RestaurantProsCons.type, RestaurantProsCons.text)
        .order_by(desc("cnt"))
    )
    pros_cons_rows = (await db.execute(pros_cons_stmt)).all()

    pros_top: list[dict] = []
    cons_top: list[dict] = []
    for type_value, text_value, cnt in pros_cons_rows:
        bucket = pros_top if type_value == ProsConsType.pro else cons_top
        if len(bucket) < top_limit:
            bucket.append({"text": text_value, "count": cnt})

    dim_stmt = (
        select(
            RestaurantRatingDimension.dimension,
            func.avg(RestaurantRatingDimension.score).label("avg_score"),
            func.count().label("cnt"),
        )
        .where(RestaurantRatingDimension.restaurant_id == restaurant_id)
        .group_by(RestaurantRatingDimension.dimension)
    )
    dim_rows = (await db.execute(dim_stmt)).all()
    dimension_averages: dict[str, dict] = {}
    for dimension, avg_score, cnt in dim_rows:
        dimension_averages[dimension.value] = {
            "average": (
                Decimal(avg_score).quantize(Decimal("0.01")) if avg_score is not None else None
            ),
            "count": cnt,
        }

    dishes_count_stmt = select(func.count(Dish.id)).where(
        Dish.restaurant_id == restaurant_id
    )
    dishes_count = (await db.execute(dishes_count_stmt)).scalar_one()

    reviews_count_stmt = (
        select(func.count(DishReview.id))
        .join(Dish, Dish.id == DishReview.dish_id)
        .where(Dish.restaurant_id == restaurant_id)
    )
    reviews_count = (await db.execute(reviews_count_stmt)).scalar_one()

    photos_count_stmt = (
        select(func.count(DishReviewImage.id))
        .join(DishReview, DishReview.id == DishReviewImage.dish_review_id)
        .join(Dish, Dish.id == DishReview.dish_id)
        .where(Dish.restaurant_id == restaurant_id)
    )
    ugc_photos_count = (await db.execute(photos_count_stmt)).scalar_one()
    dish_covers_count_stmt = select(func.count(Dish.id)).where(
        Dish.restaurant_id == restaurant_id,
        Dish.cover_image_url.is_not(None),
    )
    dish_covers_count = (await db.execute(dish_covers_count_stmt)).scalar_one()
    photos_count = ugc_photos_count + dish_covers_count

    return {
        "pros_top": pros_top,
        "cons_top": cons_top,
        "dimension_averages": dimension_averages,
        "photos_count": photos_count,
        "dishes_count": dishes_count,
        "reviews_count": reviews_count,
    }


def _encode_cursor(uploaded_at: datetime, image_id: uuid.UUID) -> str:
    raw = f"{uploaded_at.isoformat()}|{image_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode()).decode()
    iso_part, id_part = raw.split("|", 1)
    return datetime.fromisoformat(iso_part), uuid.UUID(id_part)


async def get_restaurant_photos(
    db: AsyncSession,
    restaurant_id: uuid.UUID,
    *,
    limit: int = 24,
    cursor: str | None = None,  # noqa: ARG001 — pagination kept for future
) -> dict:
    """Photos for a restaurant — UGC review uploads + dish covers, deduped by URL.

    Two sources are merged because most imported restaurants only have
    `dishes.cover_image_url` populated (no `dish_review_images` rows yet):
      1. dish_review_images (user-uploaded photos on a review)
      2. dishes.cover_image_url (one per dish — the canonical photo)
    """

    ugc_stmt = (
        select(
            DishReviewImage.id.label("photo_id"),
            DishReviewImage.url,
            DishReviewImage.alt_text,
            DishReviewImage.uploaded_at,
            Dish.id.label("dish_id"),
            Dish.name.label("dish_name"),
            DishReview.id.label("review_id"),
            User.id.label("user_id"),
            User.handle,
            User.display_name,
            DishReview.is_anonymous,
        )
        .join(DishReview, DishReview.id == DishReviewImage.dish_review_id)
        .join(Dish, Dish.id == DishReview.dish_id)
        .join(User, User.id == DishReview.user_id)
        .where(Dish.restaurant_id == restaurant_id)
        .order_by(DishReviewImage.uploaded_at.desc())
    )
    ugc_rows = (await db.execute(ugc_stmt)).all()

    items: list[dict] = []
    seen_urls: set[str] = set()

    for row in ugc_rows:
        if row.url in seen_urls:
            continue
        seen_urls.add(row.url)
        items.append(
            {
                "id": row.photo_id,
                "url": row.url,
                "alt_text": row.alt_text,
                "taken_at": row.uploaded_at,
                "dish_id": row.dish_id,
                "dish_name": row.dish_name,
                "review_id": row.review_id,
                "user_id": row.user_id,
                "user_handle": None if row.is_anonymous else row.handle,
                "user_display_name": "Anónimo" if row.is_anonymous else row.display_name,
            }
        )
        if len(items) >= limit:
            return {"items": items, "next_cursor": None}

    # Top up with dish covers. Use the dish creator as the "user" since dish
    # covers were imported, not posted by a specific reviewer. Attach the
    # latest review_id for the dish (if any) so the photo links to a review.
    latest_review_per_dish = (
        select(
            DishReview.dish_id,
            func.max(DishReview.created_at).label("latest_at"),
        )
        .group_by(DishReview.dish_id)
        .subquery()
    )
    dish_cover_stmt = (
        select(
            Dish.id.label("dish_id"),
            Dish.name.label("dish_name"),
            Dish.cover_image_url,
            Dish.created_at,
            Dish.created_by,
            User.handle,
            User.display_name,
            DishReview.id.label("review_id"),
        )
        .join(User, User.id == Dish.created_by)
        .outerjoin(
            latest_review_per_dish,
            latest_review_per_dish.c.dish_id == Dish.id,
        )
        .outerjoin(
            DishReview,
            (DishReview.dish_id == Dish.id)
            & (DishReview.created_at == latest_review_per_dish.c.latest_at),
        )
        .where(
            Dish.restaurant_id == restaurant_id,
            Dish.cover_image_url.is_not(None),
        )
        .order_by(Dish.created_at.desc())
    )
    cover_rows = (await db.execute(dish_cover_stmt)).all()

    for row in cover_rows:
        if row.cover_image_url in seen_urls:
            continue
        seen_urls.add(row.cover_image_url)
        items.append(
            {
                "id": row.dish_id,
                "url": row.cover_image_url,
                "alt_text": row.dish_name,
                "taken_at": row.created_at,
                "dish_id": row.dish_id,
                "dish_name": row.dish_name,
                "review_id": row.review_id,
                "user_id": row.created_by,
                "user_handle": row.handle,
                "user_display_name": row.display_name,
            }
        )
        if len(items) >= limit:
            break

    return {"items": items, "next_cursor": None}


async def get_restaurant_diary_stats(
    db: AsyncSession, restaurant_id: uuid.UUID
) -> dict:
    """Diary visits statistics."""

    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

    counts_stmt = select(
        func.count(VisitDiaryEntry.id).label("total"),
        func.count(func.distinct(VisitDiaryEntry.created_by)).label("unique"),
        func.count(
            case((VisitDiaryEntry.created_at >= seven_days_ago, 1))
        ).label("last_7d"),
    ).where(VisitDiaryEntry.restaurant_id == restaurant_id)
    counts_row = (await db.execute(counts_stmt)).one()

    most_ordered_stmt = (
        select(
            Dish.id,
            Dish.name,
            func.count(DishReview.id).label("rc"),
        )
        .join(DishReview, DishReview.dish_id == Dish.id)
        .where(Dish.restaurant_id == restaurant_id)
        .group_by(Dish.id, Dish.name)
        .order_by(desc("rc"))
        .limit(1)
    )
    most_ordered_row = (await db.execute(most_ordered_stmt)).first()
    most_ordered_dish = (
        {
            "id": most_ordered_row.id,
            "name": most_ordered_row.name,
            "review_count": most_ordered_row.rc,
        }
        if most_ordered_row and most_ordered_row.rc > 0
        else None
    )

    recent_visitors_stmt = (
        select(
            User.id,
            User.handle,
            User.display_name,
            User.avatar_url,
            func.max(VisitDiaryEntry.created_at).label("last_visit"),
        )
        .join(VisitDiaryEntry, VisitDiaryEntry.created_by == User.id)
        .where(VisitDiaryEntry.restaurant_id == restaurant_id)
        .group_by(User.id, User.handle, User.display_name, User.avatar_url)
        .order_by(desc("last_visit"))
        .limit(8)
    )
    recent_visitors_rows = (await db.execute(recent_visitors_stmt)).all()
    recent_visitors = [
        {
            "id": row.id,
            "handle": row.handle,
            "display_name": row.display_name,
            "avatar_url": row.avatar_url,
        }
        for row in recent_visitors_rows
    ]

    return {
        "unique_visitors": counts_row.unique or 0,
        "visits_total": counts_row.total or 0,
        "visits_last_7d": counts_row.last_7d or 0,
        "most_ordered_dish": most_ordered_dish,
        "recent_visitors": recent_visitors,
    }


async def get_nearby_restaurants(
    db: AsyncSession,
    *,
    latitude: Decimal,
    longitude: Decimal,
    exclude_restaurant_id: uuid.UUID | None = None,
    radius_km: float = 3.0,
    limit: int = 6,
) -> list[dict]:
    """Restaurants within `radius_km` of (lat, lng), ordered by distance.

    Uses Haversine via SQLAlchemy expressions — no Postgres extension required.
    For larger datasets the right move is `cube + earthdistance` with a GIST
    index — postponed until cardinality justifies it.
    """
    from app.models.restaurant import Restaurant

    lat_param = float(latitude)
    lng_param = float(longitude)

    delta_lat = func.radians(
        cast(Restaurant.latitude, Float) - lat_param
    ) / 2.0
    delta_lng = func.radians(
        cast(Restaurant.longitude, Float) - lng_param
    ) / 2.0
    a = (
        func.power(func.sin(delta_lat), 2)
        + func.cos(func.radians(lat_param))
        * func.cos(func.radians(cast(Restaurant.latitude, Float)))
        * func.power(func.sin(delta_lng), 2)
    )
    distance_km_expr = (6371.0 * 2.0 * func.asin(func.sqrt(a))).label(
        "distance_km"
    )

    stmt = (
        select(Restaurant, distance_km_expr)
        .where(
            Restaurant.latitude.is_not(None),
            Restaurant.longitude.is_not(None),
        )
        .order_by(distance_km_expr.asc())
    )

    if exclude_restaurant_id is not None:
        stmt = stmt.where(Restaurant.id != exclude_restaurant_id)

    stmt = stmt.options(selectinload(Restaurant.category)).limit(limit * 4)
    rows = (await db.execute(stmt)).all()

    out: list[dict] = []
    for restaurant, distance_km in rows:
        if distance_km is None or float(distance_km) > radius_km:
            continue
        google_photo_url: str | None = None
        if restaurant.google_photos:
            for p in restaurant.google_photos:
                if isinstance(p, dict) and p.get("url"):
                    google_photo_url = p["url"]
                    break
        out.append(
            {
                "id": restaurant.id,
                "slug": restaurant.slug,
                "name": restaurant.name,
                "location_name": restaurant.location_name,
                "cover_image_url": restaurant.cover_image_url,
                "google_photo_url": google_photo_url,
                "computed_rating": restaurant.computed_rating,
                "review_count": restaurant.review_count,
                "category": restaurant.category,
                "distance_km": round(float(distance_km), 2),
            }
        )
        if len(out) >= limit:
            break
    return out


async def get_signature_dishes(
    db: AsyncSession, restaurant_id: uuid.UUID, *, limit: int = 4
) -> list[dict]:
    """Top dishes by computed_rating with their best 5★-ish quote."""

    dishes_stmt = (
        select(Dish)
        .where(Dish.restaurant_id == restaurant_id, Dish.review_count > 0)
        .order_by(desc(Dish.computed_rating), desc(Dish.review_count))
        .limit(limit)
    )
    dishes = list((await db.execute(dishes_stmt)).scalars().all())

    items: list[dict] = []
    for dish in dishes:
        best_review_stmt = (
            select(DishReview, User.display_name, User.handle, User.id, DishReview.is_anonymous)
            .join(User, User.id == DishReview.user_id)
            .where(DishReview.dish_id == dish.id)
            .order_by(desc(DishReview.rating), desc(DishReview.created_at))
            .limit(1)
        )
        best_row = (await db.execute(best_review_stmt)).first()
        best_quote: str | None = None
        best_quote_author: str | None = None
        if best_row is not None:
            review = best_row[0]
            best_quote = (review.note or "").strip()[:240] or None
            if not review.is_anonymous:
                best_quote_author = best_row[1]

        items.append(
            {
                "id": dish.id,
                "name": dish.name,
                "cover_image_url": dish.cover_image_url,
                "computed_rating": dish.computed_rating,
                "review_count": dish.review_count,
                "best_quote": best_quote,
                "best_quote_author": best_quote_author,
            }
        )

    return items
