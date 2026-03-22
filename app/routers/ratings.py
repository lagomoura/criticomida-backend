from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.restaurant import Restaurant, RestaurantRatingDimension
from app.models.user import User
from app.schemas.restaurant import RatingDimensionCreate, RatingDimensionResponse
from app.services.rating_service import update_restaurant_rating

router = APIRouter(tags=["ratings"])


class AggregatedDimensionRating(RatingDimensionResponse):
    """Extended response with user display_name."""
    user_display_name: str | None = None


@router.get(
    "/api/restaurants/{slug}/ratings",
    response_model=dict,
)
async def get_ratings(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    # Verify restaurant exists
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == slug)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    # Get all dimension ratings with user info
    dim_result = await db.execute(
        select(RestaurantRatingDimension, User.display_name)
        .join(User, RestaurantRatingDimension.user_id == User.id)
        .where(RestaurantRatingDimension.restaurant_id == restaurant.id)
    )
    rows = dim_result.all()

    # Compute averages per dimension
    dimension_totals: dict[str, list[Decimal]] = {}
    user_breakdown: dict[str, list[dict]] = {}

    for dim_rating, display_name in rows:
        dim_name = dim_rating.dimension.value
        score = Decimal(str(dim_rating.score))

        if dim_name not in dimension_totals:
            dimension_totals[dim_name] = []
        dimension_totals[dim_name].append(score)

        user_id_str = str(dim_rating.user_id)
        if user_id_str not in user_breakdown:
            user_breakdown[user_id_str] = []
        user_breakdown[user_id_str].append({
            "dimension": dim_name,
            "score": float(score),
            "user_display_name": display_name,
        })

    averages = {}
    for dim_name, scores in dimension_totals.items():
        avg = sum(scores) / len(scores)
        averages[dim_name] = float(avg.quantize(Decimal("0.01")))

    return {
        "restaurant_id": str(restaurant.id),
        "averages": averages,
        "user_breakdown": user_breakdown,
    }


@router.put(
    "/api/restaurants/{slug}/ratings",
    response_model=list[RatingDimensionResponse],
)
async def set_ratings(
    slug: str,
    ratings: list[RatingDimensionCreate],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[RestaurantRatingDimension]:
    # Verify restaurant exists
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == slug)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    table = RestaurantRatingDimension.__table__
    saved_ids: list[int] = []

    for rating_data in ratings:
        ins = pg_insert(table).values(
            restaurant_id=restaurant.id,
            user_id=current_user.id,
            dimension=rating_data.dimension,
            score=rating_data.score,
        )
        stmt = ins.on_conflict_do_update(
            constraint='uq_rest_user_dimension',
            set_={'score': ins.excluded.score},
        ).returning(table.c.id)
        row_id = (await db.execute(stmt)).scalar_one()
        saved_ids.append(row_id)

    dim_rows = await db.execute(
        select(RestaurantRatingDimension).where(
            RestaurantRatingDimension.id.in_(saved_ids)
        )
    )
    by_id = {row.id: row for row in dim_rows.scalars().all()}
    saved_ratings = [by_id[i] for i in saved_ids]

    # Recompute restaurant rating
    await update_restaurant_rating(db, restaurant.id)

    return saved_ratings
