import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.social import Notification
from app.models.user import User
from app.schemas.notification import (
    NotificationActor,
    NotificationResponse,
    NotificationsPage,
    UnreadCountResponse,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=NotificationsPage)
async def list_notifications(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
) -> NotificationsPage:
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    stmt = (
        select(Notification, User)
        .join(User, Notification.actor_user_id == User.id)
        .where(Notification.recipient_user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit + 1)
    )
    if cursor_dt is not None:
        stmt = stmt.where(Notification.created_at < cursor_dt)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]

    items = [
        NotificationResponse(
            id=n.id,
            kind=n.kind,  # type: ignore[arg-type]
            unread=n.read_at is None,
            created_at=n.created_at,
            actor=NotificationActor(
                id=actor.id,
                display_name=actor.display_name,
                handle=actor.handle,
                avatar_url=actor.avatar_url,
            ),
            target_review_id=n.target_review_id,
            target_user_id=n.target_user_id,
            target_restaurant_id=n.target_restaurant_id,
            text=n.text,
        )
        for n, actor in trimmed
    ]
    next_cursor = trimmed[-1][0].created_at.isoformat() if has_more and trimmed else None
    return NotificationsPage(items=items, next_cursor=next_cursor)


@router.get("/unread-count", response_model=UnreadCountResponse)
async def unread_count(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UnreadCountResponse:
    result = await db.execute(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.recipient_user_id == current_user.id,
            Notification.read_at.is_(None),
        )
    )
    return UnreadCountResponse(unread=int(result.scalar_one() or 0))


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    notification_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_user_id == current_user.id,
        )
    )
    notif = result.scalar_one_or_none()
    if notif is None:
        raise HTTPException(status_code=404, detail="Notificación no encontrada")
    if notif.read_at is None:
        notif.read_at = datetime.now(timezone.utc)
        await db.commit()


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Notification)
        .where(
            Notification.recipient_user_id == current_user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=now)
    )
    await db.commit()
