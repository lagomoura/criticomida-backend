import base64
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Float, case, cast, desc, func, select
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
