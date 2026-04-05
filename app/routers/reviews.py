import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.dish import (
    Dish,
    DishReview,
    DishReviewImage,
    DishReviewProsCons,
    DishReviewTag,
)
from app.models.user import User, UserRole
from app.schemas.dish import DishReviewCreate, DishReviewResponse, DishReviewUpdate, MyReviewResponse
from app.services.rating_service import update_dish_rating, update_restaurant_rating

router = APIRouter(tags=["reviews"])


@router.get(
    "/api/users/me/reviews",
    response_model=list[MyReviewResponse],
)
async def get_my_reviews(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[DishReview]:
    result = await db.execute(
        select(DishReview)
        .options(
            selectinload(DishReview.user),
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
            selectinload(DishReview.images),
            selectinload(DishReview.dish).selectinload(Dish.restaurant),
        )
        .where(DishReview.user_id == current_user.id)
        .order_by(DishReview.created_at.desc())
    )
    return list(result.scalars().all())


async def _load_review_with_relations(
    db: AsyncSession, review_id: uuid.UUID
) -> DishReview | None:
    result = await db.execute(
        select(DishReview)
        .options(
            selectinload(DishReview.user),
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
            selectinload(DishReview.images),
        )
        .where(DishReview.id == review_id)
    )
    return result.scalar_one_or_none()


@router.get(
    "/api/dishes/{dish_id}/reviews",
    response_model=list[DishReviewResponse],
)
async def list_reviews(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[DishReview]:
    # Verify dish exists
    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    if dish_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )

    result = await db.execute(
        select(DishReview)
        .options(
            selectinload(DishReview.user),
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
            selectinload(DishReview.images),
        )
        .where(DishReview.dish_id == dish_id)
        .order_by(DishReview.created_at.desc())
    )
    return list(result.scalars().all())


@router.post(
    "/api/dishes/{dish_id}/reviews",
    response_model=DishReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(
    dish_id: uuid.UUID,
    review_data: DishReviewCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishReview:
    # Verify dish exists and get restaurant_id
    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    dish = dish_result.scalar_one_or_none()
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )

    review = DishReview(
        dish_id=dish_id,
        user_id=current_user.id,
        date_tasted=review_data.date_tasted,
        time_tasted=review_data.time_tasted,
        note=review_data.note,
        rating=review_data.rating,
        portion_size=review_data.portion_size,
        would_order_again=review_data.would_order_again,
        visited_with=review_data.visited_with,
        is_anonymous=review_data.is_anonymous,
    )
    db.add(review)

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already reviewed this dish",
        )

    # Add pros/cons
    for pc in review_data.pros_cons:
        db.add(DishReviewProsCons(
            dish_review_id=review.id,
            type=pc.type,
            text=pc.text,
        ))

    # Add tags
    for tag_data in review_data.tags:
        db.add(DishReviewTag(
            dish_review_id=review.id,
            tag=tag_data.tag,
        ))

    # Add images
    for img_data in review_data.images:
        db.add(DishReviewImage(
            dish_review_id=review.id,
            url=img_data.url,
            alt_text=img_data.alt_text,
            display_order=img_data.display_order,
        ))

    await db.flush()

    # Recompute ratings
    await update_dish_rating(db, dish_id)
    await update_restaurant_rating(db, dish.restaurant_id)

    # Reload with relationships
    loaded = await _load_review_with_relations(db, review.id)
    return loaded  # type: ignore[return-value]


@router.put(
    "/api/dish-reviews/{review_id}",
    response_model=DishReviewResponse,
)
async def update_review(
    review_id: uuid.UUID,
    review_data: DishReviewUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishReview:
    result = await db.execute(
        select(DishReview).where(DishReview.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    # Only the author can update their review
    if review.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own reviews",
        )

    update_data = review_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(review, field, value)

    await db.flush()

    # Recompute ratings
    await update_dish_rating(db, review.dish_id)

    # Get restaurant_id through dish
    dish_result = await db.execute(select(Dish).where(Dish.id == review.dish_id))
    dish = dish_result.scalar_one()
    await update_restaurant_rating(db, dish.restaurant_id)

    loaded = await _load_review_with_relations(db, review.id)
    return loaded  # type: ignore[return-value]


@router.delete(
    "/api/dish-reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_review(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    result = await db.execute(
        select(DishReview).where(DishReview.id == review_id)
    )
    review = result.scalar_one_or_none()
    if review is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Review not found",
        )

    # Author or admin can delete
    if review.user_id != current_user.id and current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own reviews",
        )

    dish_id = review.dish_id

    # Get restaurant_id before deleting
    dish_result = await db.execute(select(Dish).where(Dish.id == dish_id))
    dish = dish_result.scalar_one()
    restaurant_id = dish.restaurant_id

    await db.delete(review)
    await db.flush()

    # Recompute ratings
    await update_dish_rating(db, dish_id)
    await update_restaurant_rating(db, restaurant_id)
