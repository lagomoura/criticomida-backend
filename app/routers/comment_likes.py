import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import LIKE_LIMIT, limiter
from app.models.like import CommentLike
from app.models.social import Comment
from app.models.user import User
from app.schemas.social import CommentLikeActionResponse
from app.services.notification_service import record_comment_like_notification

router = APIRouter(prefix="/api/comments", tags=["comment_likes"])


async def _likes_count(db: AsyncSession, comment_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(CommentLike)
        .where(CommentLike.comment_id == comment_id)
    )
    return int(result.scalar_one() or 0)


async def _load_comment(db: AsyncSession, comment_id: uuid.UUID) -> Comment:
    """Returns the comment if active; raises 404 otherwise."""
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None or comment.removed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comentario no encontrado",
        )
    return comment


@router.post("/{comment_id}/like", response_model=CommentLikeActionResponse)
@limiter.limit(LIKE_LIMIT)
async def like_comment(
    request: Request,
    comment_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommentLikeActionResponse:
    """Idempotent: liking an already-liked comment is a no-op."""
    comment = await _load_comment(db, comment_id)

    existing = await db.execute(
        select(CommentLike).where(
            CommentLike.user_id == current_user.id,
            CommentLike.comment_id == comment_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(CommentLike(user_id=current_user.id, comment_id=comment_id))
        await record_comment_like_notification(
            db,
            actor_id=current_user.id,
            comment_id=comment.id,
            comment_owner_id=comment.user_id,
            comment_body=comment.body,
            review_id=comment.review_id,
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()

    count = await _likes_count(db, comment_id)
    return CommentLikeActionResponse(
        comment_id=comment_id, liked=True, likes_count=count
    )


@router.delete("/{comment_id}/like", response_model=CommentLikeActionResponse)
@limiter.limit(LIKE_LIMIT)
async def unlike_comment(
    request: Request,
    comment_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommentLikeActionResponse:
    """Idempotent: unliking an un-liked comment is a no-op."""
    await _load_comment(db, comment_id)

    existing = await db.execute(
        select(CommentLike).where(
            CommentLike.user_id == current_user.id,
            CommentLike.comment_id == comment_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()

    count = await _likes_count(db, comment_id)
    return CommentLikeActionResponse(
        comment_id=comment_id, liked=False, likes_count=count
    )
