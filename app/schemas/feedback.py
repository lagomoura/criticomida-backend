import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.user_feedback import FeedbackCategory, FeedbackStatus


class UserFeedbackCreate(BaseModel):
    category: FeedbackCategory
    message: str = Field(min_length=1, max_length=10000)


class UserFeedbackResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    category: FeedbackCategory
    message: str
    status: FeedbackStatus
    created_at: datetime

    model_config = {'from_attributes': True}
