import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import LIKE_LIMIT, limiter
from app.models.dish import DishReview
from app.models.like import Like
from app.models.user import User
from app.schemas.social import LikeActionResponse
from app.services.notification_service import record_like_notification

router = APIRouter(prefix="/api/reviews", tags=["likes"])


async def _likes_count(db: AsyncSession, review_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(Like).where(Like.review_id == review_id)
    )
    return int(result.scalar_one() or 0)


async def _review_owner(db: AsyncSession, review_id: uuid.UUID) -> uuid.UUID:
    """Returns the review's author id; raises 404 if the review does not exist."""
    result = await db.execute(
        select(DishReview.user_id).where(DishReview.id == review_id)
    )
    owner = result.scalar_one_or_none()
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reseña no encontrada",
        )
    return owner


@router.post("/{review_id}/like", response_model=LikeActionResponse)
@limiter.limit(LIKE_LIMIT)
async def like_review(
    request: Request,
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LikeActionResponse:
    """Idempotent: liking an already-liked review returns the same counts."""
    owner_id = await _review_owner(db, review_id)

    existing = await db.execute(
        select(Like).where(Like.user_id == current_user.id, Like.review_id == review_id)
    )
    if existing.scalar_one_or_none() is None:
        db.add(Like(user_id=current_user.id, review_id=review_id))
        await record_like_notification(
            db,
            actor_id=current_user.id,
            review_id=review_id,
            review_owner_id=owner_id,
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()

    count = await _likes_count(db, review_id)
    return LikeActionResponse(review_id=review_id, liked=True, likes_count=count)


@router.delete("/{review_id}/like", response_model=LikeActionResponse)
@limiter.limit(LIKE_LIMIT)
async def unlike_review(
    request: Request,
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LikeActionResponse:
    """Idempotent: unliking an un-liked review is a no-op."""
    await _review_owner(db, review_id)

    existing = await db.execute(
        select(Like).where(Like.user_id == current_user.id, Like.review_id == review_id)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()

    count = await _likes_count(db, review_id)
    return LikeActionResponse(review_id=review_id, liked=False, likes_count=count)
