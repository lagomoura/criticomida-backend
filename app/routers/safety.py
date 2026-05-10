"""Block / mute endpoints.

POST/DELETE /api/users/{id_or_handle}/block — bidirectional impact:
the blocked user can't follow, comment, or notify; neither side sees
the other in feeds. POSTing a block also auto-removes any follow
relationship in either direction so el grafo queda consistente.

POST/DELETE /api/users/{id_or_handle}/mute — silent unidirectional:
the muted user is unaware. Solo afecta lo que el muter recibe.

GET /api/users/me/blocked y /api/users/me/muted — listas paginadas
por created_at del block/mute (DESC).
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import FOLLOW_LIMIT, limiter
from app.models.follow import Follow
from app.models.social import UserBlock, UserMute
from app.models.user import User
from app.routers.follows import _resolve_user
from app.schemas.safety import (
    BlockActionResponse,
    MuteActionResponse,
    SafetyUserSummary,
    SafetyUsersPage,
)

router = APIRouter(prefix="/api/users", tags=["safety"])


def _ensure_not_self(target_id: uuid.UUID, viewer_id: uuid.UUID, action: str) -> None:
    if target_id == viewer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No podés {action} a vos mismo.",
        )


@router.post("/{id_or_handle}/block", response_model=BlockActionResponse)
@limiter.limit(FOLLOW_LIMIT)
async def block_user(
    request: Request,
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BlockActionResponse:
    """Idempotent. Borra los Follow rows en ambas direcciones para que
    el grafo no quede en estado inconsistente (sigo a alguien que
    bloqueé, etc.)."""
    target = await _resolve_user(db, id_or_handle)
    _ensure_not_self(target.id, current_user.id, "bloquear")

    existing = await db.execute(
        select(UserBlock).where(
            UserBlock.blocker_id == current_user.id,
            UserBlock.blocked_id == target.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(UserBlock(blocker_id=current_user.id, blocked_id=target.id))
        # Borrar cualquier follow entre las dos partes (en ambas
        # direcciones). El bloqueado deja de seguir al bloqueante y
        # viceversa — ningún lado debería seguir viendo al otro tras
        # un block.
        await db.execute(
            delete(Follow).where(
                or_(
                    and_(
                        Follow.follower_id == current_user.id,
                        Follow.following_id == target.id,
                    ),
                    and_(
                        Follow.follower_id == target.id,
                        Follow.following_id == current_user.id,
                    ),
                )
            )
        )
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()

    return BlockActionResponse(
        blocker_id=current_user.id,
        blocked_id=target.id,
        blocked=True,
    )


@router.delete("/{id_or_handle}/block", response_model=BlockActionResponse)
@limiter.limit(FOLLOW_LIMIT)
async def unblock_user(
    request: Request,
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BlockActionResponse:
    target = await _resolve_user(db, id_or_handle)
    _ensure_not_self(target.id, current_user.id, "desbloquear")

    existing = await db.execute(
        select(UserBlock).where(
            UserBlock.blocker_id == current_user.id,
            UserBlock.blocked_id == target.id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.flush()

    return BlockActionResponse(
        blocker_id=current_user.id,
        blocked_id=target.id,
        blocked=False,
    )


@router.post("/{id_or_handle}/mute", response_model=MuteActionResponse)
@limiter.limit(FOLLOW_LIMIT)
async def mute_user(
    request: Request,
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MuteActionResponse:
    target = await _resolve_user(db, id_or_handle)
    _ensure_not_self(target.id, current_user.id, "silenciar")

    existing = await db.execute(
        select(UserMute).where(
            UserMute.muter_id == current_user.id,
            UserMute.muted_id == target.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(UserMute(muter_id=current_user.id, muted_id=target.id))
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()

    return MuteActionResponse(
        muter_id=current_user.id,
        muted_id=target.id,
        muted=True,
    )


@router.delete("/{id_or_handle}/mute", response_model=MuteActionResponse)
@limiter.limit(FOLLOW_LIMIT)
async def unmute_user(
    request: Request,
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MuteActionResponse:
    target = await _resolve_user(db, id_or_handle)
    _ensure_not_self(target.id, current_user.id, "des-silenciar")

    existing = await db.execute(
        select(UserMute).where(
            UserMute.muter_id == current_user.id,
            UserMute.muted_id == target.id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.flush()

    return MuteActionResponse(
        muter_id=current_user.id,
        muted_id=target.id,
        muted=False,
    )


def _parse_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        return datetime.fromisoformat(cursor)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cursor inválido",
        )


@router.get("/me/blocked", response_model=SafetyUsersPage)
async def list_blocked_users(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> SafetyUsersPage:
    cursor_dt = _parse_cursor(cursor)
    stmt = (
        select(User, UserBlock.created_at)
        .join(UserBlock, UserBlock.blocked_id == User.id)
        .where(UserBlock.blocker_id == current_user.id)
        .order_by(UserBlock.created_at.desc())
        .limit(limit + 1)
    )
    if cursor_dt is not None:
        stmt = stmt.where(UserBlock.created_at < cursor_dt)
    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]
    items = [
        SafetyUserSummary(
            id=user.id,
            display_name=user.display_name,
            handle=user.handle,
            avatar_url=user.avatar_url,
            created_at=created_at,
        )
        for user, created_at in trimmed
    ]
    next_cursor = trimmed[-1][1].isoformat() if has_more and trimmed else None
    return SafetyUsersPage(items=items, next_cursor=next_cursor)


@router.get("/me/muted", response_model=SafetyUsersPage)
async def list_muted_users(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> SafetyUsersPage:
    cursor_dt = _parse_cursor(cursor)
    stmt = (
        select(User, UserMute.created_at)
        .join(UserMute, UserMute.muted_id == User.id)
        .where(UserMute.muter_id == current_user.id)
        .order_by(UserMute.created_at.desc())
        .limit(limit + 1)
    )
    if cursor_dt is not None:
        stmt = stmt.where(UserMute.created_at < cursor_dt)
    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]
    items = [
        SafetyUserSummary(
            id=user.id,
            display_name=user.display_name,
            handle=user.handle,
            avatar_url=user.avatar_url,
            created_at=created_at,
        )
        for user, created_at in trimmed
    ]
    next_cursor = trimmed[-1][1].isoformat() if has_more and trimmed else None
    return SafetyUsersPage(items=items, next_cursor=next_cursor)
