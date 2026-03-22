import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.dish import Dish
from app.models.image import Image
from app.models.menu import Menu
from app.models.restaurant import (
    Restaurant,
    RestaurantProsCons,
    RestaurantRatingDimension,
    VisitDiaryEntry,
)


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


async def get_restaurant_detail(
    db: AsyncSession, slug: str
) -> Restaurant | None:
    """Return full restaurant detail with all relationships loaded."""
    stmt = (
        select(Restaurant)
        .options(
            selectinload(Restaurant.category),
            selectinload(Restaurant.creator),
            selectinload(Restaurant.dishes).selectinload(Dish.reviews),
            selectinload(Restaurant.dimension_ratings),
            selectinload(Restaurant.pros_cons),
            selectinload(Restaurant.diary_entries),
            selectinload(Restaurant.menu),
        )
        .where(Restaurant.slug == slug)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_restaurant_by_slug(
    db: AsyncSession, slug: str
) -> Restaurant | None:
    """Get a restaurant by slug without eager loading."""
    result = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
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
