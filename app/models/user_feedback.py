import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FeedbackCategory(str, enum.Enum):
    bug = 'bug'
    feature = 'feature'
    general = 'general'


class FeedbackStatus(str, enum.Enum):
    open = 'open'
    read = 'read'
    closed = 'closed'


class UserFeedback(Base):
    __tablename__ = 'user_feedback'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('users.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    category: Mapped[FeedbackCategory] = mapped_column(
        Enum(FeedbackCategory, name='feedback_category'),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[FeedbackStatus] = mapped_column(
        Enum(FeedbackStatus, name='feedback_status'),
        default=FeedbackStatus.open,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user: Mapped['User | None'] = relationship(back_populates='feedback_submissions')  # noqa: F821
