import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import Dish, DishReview


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
