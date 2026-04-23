import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.follow import Follow
from app.models.user import User
from app.schemas.social import (
    FollowActionResponse,
    FollowerSummary,
    FollowersPage,
)
from app.services.notification_service import record_follow_notification

router = APIRouter(prefix="/api/users", tags=["follows"])


async def _resolve_user(db: AsyncSession, id_or_handle: str) -> User:
    """Find a user by UUID or by lowercase handle. 404 otherwise."""
    user: User | None = None
    try:
        user_uuid = uuid.UUID(id_or_handle)
        result = await db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
    except ValueError:
        handle = id_or_handle.lower().strip()
        if handle:
            result = await db.execute(select(User).where(User.handle == handle))
            user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )
    return user


async def _followers_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Follow)
        .where(Follow.following_id == user_id)
    )
    return int(result.scalar_one() or 0)


@router.post("/{id_or_handle}/follow", response_model=FollowActionResponse)
async def follow_user(
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FollowActionResponse:
    """Idempotent: following an already-followed user returns the same result."""
    target = await _resolve_user(db, id_or_handle)
    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No podés seguirte a vos mismo.",
        )

    existing = await db.execute(
        select(Follow).where(
            Follow.follower_id == current_user.id,
            Follow.following_id == target.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(Follow(follower_id=current_user.id, following_id=target.id))
        await record_follow_notification(
            db, actor_id=current_user.id, target_user_id=target.id
        )
        try:
            await db.commit()
        except IntegrityError:
            # Concurrent insert race — the other insert won, we're already
            # following; proceed as if the call succeeded.
            await db.rollback()

    followers = await _followers_count(db, target.id)
    return FollowActionResponse(
        follower_id=current_user.id,
        following_id=target.id,
        following=True,
        followers_count=followers,
    )


@router.delete("/{id_or_handle}/follow", response_model=FollowActionResponse)
async def unfollow_user(
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FollowActionResponse:
    """Idempotent: unfollowing a user you don't follow returns the same result."""
    target = await _resolve_user(db, id_or_handle)

    existing = await db.execute(
        select(Follow).where(
            Follow.follower_id == current_user.id,
            Follow.following_id == target.id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()

    followers = await _followers_count(db, target.id)
    return FollowActionResponse(
        follower_id=current_user.id,
        following_id=target.id,
        following=False,
        followers_count=followers,
    )


@router.get("/{id_or_handle}/followers", response_model=FollowersPage)
async def list_followers(
    id_or_handle: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> FollowersPage:
    target = await _resolve_user(db, id_or_handle)
    return await _list_follow_edges(
        db,
        side="followers",
        user_id=target.id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/{id_or_handle}/following", response_model=FollowersPage)
async def list_following(
    id_or_handle: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> FollowersPage:
    target = await _resolve_user(db, id_or_handle)
    return await _list_follow_edges(
        db,
        side="following",
        user_id=target.id,
        cursor=cursor,
        limit=limit,
    )


async def _list_follow_edges(
    db: AsyncSession,
    *,
    side: str,  # "followers" | "following"
    user_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> FollowersPage:
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cursor inválido",
            )

    if side == "followers":
        # The followers of `user_id` → join Follow.follower_id → User.id.
        stmt = (
            select(User, Follow.created_at)
            .join(Follow, Follow.follower_id == User.id)
            .where(Follow.following_id == user_id)
        )
    else:
        stmt = (
            select(User, Follow.created_at)
            .join(Follow, Follow.following_id == User.id)
            .where(Follow.follower_id == user_id)
        )

    stmt = stmt.order_by(Follow.created_at.desc()).limit(limit + 1)
    if cursor_dt is not None:
        stmt = stmt.where(Follow.created_at < cursor_dt)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]

    items = [
        FollowerSummary(
            id=user.id,
            display_name=user.display_name,
            handle=user.handle,
            avatar_url=user.avatar_url,
            bio=user.bio,
            created_at=created_at,
        )
        for user, created_at in trimmed
    ]
    next_cursor = trimmed[-1][1].isoformat() if has_more and trimmed else None
    return FollowersPage(items=items, next_cursor=next_cursor)
