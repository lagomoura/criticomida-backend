import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, get_current_user_optional
from app.middleware.rate_limit import COMMENT_CREATE_LIMIT, limiter
from app.models.dish import DishReview
from app.models.social import Comment
from app.models.user import User, UserRole
from app.schemas.comment import (
    CommentAuthor,
    CommentCreate,
    CommentResponse,
    CommentsPage,
    CommentUpdate,
)
from app.services.notification_service import record_comment_notification

router = APIRouter(tags=["comments"])

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DUPLICATE_WINDOW = timedelta(minutes=10)
# Block at the 3rd identical post within the window. Counting existing rows
# before the insert: 0/1 allowed, >=2 blocked (this attempt would be the 3rd).
_DUPLICATE_THRESHOLD = 2
_MAX_URLS_PER_COMMENT = 3


async def _anti_spam_check(
    db: AsyncSession, *, user_id: uuid.UUID, body: str
) -> None:
    """Block obvious comment spam (spec §9.2).

    Trips on:
    - More than `_MAX_URLS_PER_COMMENT` URLs in the body → 400.
    - Same body posted by the same user `>= _DUPLICATE_THRESHOLD` times within
      the last `_DUPLICATE_WINDOW` → 429.
    """
    if len(_URL_RE.findall(body)) > _MAX_URLS_PER_COMMENT:
        raise HTTPException(status_code=400, detail="Demasiados enlaces")

    cutoff = datetime.now(timezone.utc) - _DUPLICATE_WINDOW
    dup_count = await db.execute(
        select(func.count())
        .select_from(Comment)
        .where(
            Comment.user_id == user_id,
            Comment.body == body,
            Comment.created_at >= cutoff,
        )
    )
    if int(dup_count.scalar_one() or 0) >= _DUPLICATE_THRESHOLD:
        raise HTTPException(
            status_code=429, detail="Comentario repetido demasiadas veces"
        )


def _comment_response(
    comment: Comment, author: User, *, viewer: User | None
) -> CommentResponse:
    is_owner = viewer is not None and viewer.id == comment.user_id
    is_admin = viewer is not None and viewer.role == UserRole.admin
    return CommentResponse(
        id=comment.id,
        review_id=comment.review_id,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        body=comment.body,
        author=CommentAuthor(
            id=author.id,
            display_name=author.display_name,
            handle=author.handle,
            avatar_url=author.avatar_url,
        ),
        can_delete=is_owner or is_admin,
        can_edit=is_owner,
        can_report=viewer is not None and not is_owner,
    )


@router.get("/api/reviews/{review_id}/comments", response_model=CommentsPage)
async def list_comments(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> CommentsPage:
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    stmt = (
        select(Comment, User)
        .join(User, Comment.user_id == User.id)
        .where(Comment.review_id == review_id)
        .where(Comment.removed_at.is_(None))
        .order_by(Comment.created_at.asc())
        .limit(limit + 1)
    )
    if cursor_dt is not None:
        stmt = stmt.where(Comment.created_at > cursor_dt)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]
    items = [_comment_response(c, u, viewer=viewer) for c, u in trimmed]
    next_cursor = trimmed[-1][0].created_at.isoformat() if has_more and trimmed else None
    return CommentsPage(items=items, next_cursor=next_cursor)


@router.post(
    "/api/reviews/{review_id}/comments",
    response_model=CommentResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(COMMENT_CREATE_LIMIT)
async def create_comment(
    request: Request,
    review_id: uuid.UUID,
    payload: CommentCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommentResponse:
    review_row = await db.execute(
        select(DishReview.id, DishReview.user_id).where(DishReview.id == review_id)
    )
    review = review_row.first()
    if review is None:
        raise HTTPException(status_code=404, detail="Reseña no encontrada")

    body = payload.body.strip()
    await _anti_spam_check(db, user_id=current_user.id, body=body)

    comment = Comment(
        review_id=review_id,
        user_id=current_user.id,
        body=body,
    )
    db.add(comment)

    await record_comment_notification(
        db,
        actor_id=current_user.id,
        review_id=review_id,
        review_owner_id=review.user_id,
        comment_body=comment.body,
    )

    await db.commit()
    await db.refresh(comment)

    return _comment_response(comment, current_user, viewer=current_user)


@router.patch(
    "/api/comments/{comment_id}", response_model=CommentResponse
)
async def update_comment(
    comment_id: uuid.UUID,
    payload: CommentUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommentResponse:
    """Edit body of own comment. Admins cannot edit other users' comments."""
    result = await db.execute(
        select(Comment, User).join(User, Comment.user_id == User.id).where(
            Comment.id == comment_id
        )
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Comentario no encontrado")
    comment, author = row
    if comment.removed_at is not None:
        raise HTTPException(status_code=404, detail="Comentario no encontrado")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="No podés editar este comentario")

    body = payload.body.strip()
    if body == comment.body:
        return _comment_response(comment, author, viewer=current_user)

    await _anti_spam_check(db, user_id=current_user.id, body=body)

    comment.body = body
    await db.commit()
    await db.refresh(comment)

    return _comment_response(comment, author, viewer=current_user)


@router.delete(
    "/api/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_comment(
    comment_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft delete. Only the author or an admin can remove."""
    result = await db.execute(select(Comment).where(Comment.id == comment_id))
    comment = result.scalar_one_or_none()
    if comment is None or comment.removed_at is not None:
        raise HTTPException(status_code=404, detail="Comentario no encontrado")
    if comment.user_id != current_user.id and current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="No podés borrar este comentario")

    comment.removed_at = datetime.now(timezone.utc)
    await db.commit()
