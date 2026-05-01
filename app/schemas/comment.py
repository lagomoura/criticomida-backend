import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class CommentUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class CommentAuthor(BaseModel):
    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None


class CommentResponse(BaseModel):
    id: uuid.UUID
    review_id: uuid.UUID
    parent_comment_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    author: CommentAuthor
    body: str
    replies_count: int = 0
    likes_count: int = 0
    viewer_liked: bool = False
    can_delete: bool = False
    can_edit: bool = False
    can_report: bool = False

    model_config = {"from_attributes": True}


class CommentsPage(BaseModel):
    items: list[CommentResponse]
    next_cursor: str | None = None
