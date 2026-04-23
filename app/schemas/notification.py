import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class NotificationActor(BaseModel):
    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None


class NotificationResponse(BaseModel):
    id: uuid.UUID
    kind: Literal["like", "comment", "follow"]
    unread: bool
    created_at: datetime
    actor: NotificationActor
    target_review_id: uuid.UUID | None = None
    target_user_id: uuid.UUID | None = None
    text: str


class NotificationsPage(BaseModel):
    items: list[NotificationResponse]
    next_cursor: str | None = None


class UnreadCountResponse(BaseModel):
    unread: int
