import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.dish import DishReview
from app.models.social import Bookmark
from app.models.user import User
from app.routers.feed import _build_feed_items
from app.schemas.bookmark_report import BookmarkActionResponse
from app.schemas.feed import FeedPage

router = APIRouter(tags=["bookmarks"])


async def _saves_count(db: AsyncSession, review_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Bookmark)
        .where(Bookmark.review_id == review_id)
    )
    return int(result.scalar_one() or 0)


async def _ensure_review(db: AsyncSession, review_id: uuid.UUID) -> None:
    result = await db.execute(select(DishReview.id).where(DishReview.id == review_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Reseña no encontrada")


@router.post(
    "/api/reviews/{review_id}/save", response_model=BookmarkActionResponse
)
async def save_review(
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BookmarkActionResponse:
    """Idempotent: saving an already-saved review returns the same count."""
    await _ensure_review(db, review_id)
    existing = await db.execute(
        select(Bookmark).where(
            Bookmark.user_id == current_user.id,
            Bookmark.review_id == review_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(Bookmark(user_id=current_user.id, review_id=review_id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()

    return BookmarkActionResponse(
        review_id=review_id,
        saved=True,
        saves_count=await _saves_count(db, review_id),
    )


@router.delete(
    "/api/reviews/{review_id}/save", response_model=BookmarkActionResponse
)
async def unsave_review(
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BookmarkActionResponse:
    await _ensure_review(db, review_id)
    existing = await db.execute(
        select(Bookmark).where(
            Bookmark.user_id == current_user.id,
            Bookmark.review_id == review_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()

    return BookmarkActionResponse(
        review_id=review_id,
        saved=False,
        saves_count=await _saves_count(db, review_id),
    )


@router.get("/api/users/me/bookmarks", response_model=FeedPage)
async def list_my_bookmarks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> FeedPage:
    """
    Returns the viewer's saved reviews hydrated as full `FeedItem`s so the
    frontend can render them with `PostCard` directly. Cursor tracks the
    `bookmark.created_at` (saved-at), not the review's creation date.
    """
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    # Find which reviews the user has saved, most-recently-saved first. We
    # fetch one extra row as a cheap has_more probe.
    bookmark_stmt = (
        select(Bookmark.review_id, Bookmark.created_at)
        .where(Bookmark.user_id == current_user.id)
        .order_by(Bookmark.created_at.desc())
        .limit(limit + 1)
    )
    if cursor_dt is not None:
        bookmark_stmt = bookmark_stmt.where(Bookmark.created_at < cursor_dt)

    bookmark_rows = (await db.execute(bookmark_stmt)).all()
    has_more = len(bookmark_rows) > limit
    trimmed = bookmark_rows[:limit]

    if not trimmed:
        return FeedPage(items=[], next_cursor=None)

    review_ids = [b.review_id for b in trimmed]
    saved_at_by_id = {b.review_id: b.created_at for b in trimmed}

    # Hydrate via the shared feed helper so the response shape matches the
    # rest of the social UI exactly.
    items, _ = await _build_feed_items(
        db,
        current_user,
        base_filters=[DishReview.id.in_(review_ids)],
        cursor_dt=None,
        limit=limit,
        with_extras=False,
    )

    # Restore the saved-at order (the feed helper orders by created_at of the
    # review, not of the bookmark).
    items.sort(key=lambda it: saved_at_by_id[it.id], reverse=True)

    next_cursor = (
        saved_at_by_id[trimmed[limit - 1].review_id].isoformat()
        if has_more and len(trimmed) == limit
        else None
    )
    return FeedPage(items=items, next_cursor=next_cursor)
