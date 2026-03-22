import uuid
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import Dish, DishReview
from app.models.restaurant import Restaurant, RestaurantRatingDimension
from app.models.user import User, UserRole


async def compute_dish_rating(db: AsyncSession, dish_id: uuid.UUID) -> Decimal:
    """
    Compute dish rating as average of all review ratings.
    Critic reviews count x2 (included twice in the average).
    """
    result = await db.execute(
        select(DishReview, User.role)
        .join(User, DishReview.user_id == User.id)
        .where(DishReview.dish_id == dish_id)
    )
    rows = result.all()

    if not rows:
        return Decimal("0")

    total = Decimal("0")
    count = 0
    for review, role in rows:
        rating = Decimal(str(review.rating))
        if role == UserRole.critic:
            # Critic reviews count x2
            total += rating * 2
            count += 2
        else:
            total += rating
            count += 1

    if count == 0:
        return Decimal("0")

    computed = (total / count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return computed


async def update_dish_rating(db: AsyncSession, dish_id: uuid.UUID) -> None:
    """Recompute and persist the dish computed_rating and review_count."""
    rating = await compute_dish_rating(db, dish_id)

    result = await db.execute(select(DishReview).where(DishReview.dish_id == dish_id))
    review_count = len(result.scalars().all())

    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    dish = dish_result.scalar_one_or_none()
    if dish:
        dish.computed_rating = rating
        dish.review_count = review_count
        await db.flush()


async def compute_restaurant_rating(
    db: AsyncSession, restaurant_id: uuid.UUID
) -> Decimal:
    """
    Compute restaurant rating:
    - If dimension ratings exist:
        avg(dish computed_ratings) * 0.5 + avg(all dimensions across users) * 0.5
    - Otherwise:
        avg(dish computed_ratings)
    - Critic dimension ratings count x2
    """
    # Get all dishes for this restaurant with their computed ratings
    dish_result = await db.execute(
        select(Dish).where(Dish.restaurant_id == restaurant_id)
    )
    dishes = dish_result.scalars().all()

    # Compute average of dish computed ratings
    dish_ratings = [d.computed_rating for d in dishes if d.computed_rating > 0]
    if not dish_ratings:
        avg_dish = Decimal("0")
    else:
        avg_dish = sum(dish_ratings) / len(dish_ratings)

    # Get dimension ratings
    dim_result = await db.execute(
        select(RestaurantRatingDimension, User.role)
        .join(User, RestaurantRatingDimension.user_id == User.id)
        .where(RestaurantRatingDimension.restaurant_id == restaurant_id)
    )
    dim_rows = dim_result.all()

    if dim_rows:
        total_dim = Decimal("0")
        dim_count = 0
        for dim_rating, role in dim_rows:
            score = Decimal(str(dim_rating.score))
            if role == UserRole.critic:
                total_dim += score * 2
                dim_count += 2
            else:
                total_dim += score
                dim_count += 1

        avg_dim = total_dim / dim_count if dim_count > 0 else Decimal("0")

        if avg_dish > 0:
            computed = avg_dish * Decimal("0.5") + avg_dim * Decimal("0.5")
        else:
            computed = avg_dim
    else:
        computed = avg_dish

    return computed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def update_restaurant_rating(
    db: AsyncSession, restaurant_id: uuid.UUID
) -> None:
    """Recompute and persist the restaurant computed_rating and review_count."""
    rating = await compute_restaurant_rating(db, restaurant_id)

    # Count total dish reviews across all dishes in this restaurant
    dish_result = await db.execute(
        select(Dish).where(Dish.restaurant_id == restaurant_id)
    )
    dishes = dish_result.scalars().all()
    total_reviews = sum(d.review_count for d in dishes)

    rest_result = await db.execute(
        select(Restaurant).where(Restaurant.id == restaurant_id)
    )
    restaurant = rest_result.scalar_one_or_none()
    if restaurant:
        restaurant.computed_rating = rating
        restaurant.review_count = total_reviews
        await db.flush()
