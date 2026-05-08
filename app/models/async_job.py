"""Persistent queue for write-behind jobs.

The queue is consumed by a single worker loop launched in
``app.main.production_lifespan`` (see ``app/services/async_job_worker.py``).

Two job kinds today, both keyed off a ``DishReview``:

- ``embed_review`` — recompute the dish/review embeddings via Gemini.
- ``sentiment_review`` — classify the review note via Gemini.

The model intentionally keeps a single payload column
(``payload_review_id``) instead of a generic JSONB:
both current jobs operate on a review, the FK gives us cascade
delete for free, and a typed column is cheaper to index than a
JSON path. When a future job type needs different inputs we can
add a sibling column or a JSONB blob then; YAGNI for now.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AsyncJobKind(str, enum.Enum):
    embed_review = "embed_review"
    sentiment_review = "sentiment_review"


class AsyncJobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class AsyncJob(Base):
    __tablename__ = "async_job"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[AsyncJobKind] = mapped_column(
        Enum(AsyncJobKind, name="async_job_kind", create_type=False),
        nullable=False,
    )
    status: Mapped[AsyncJobStatus] = mapped_column(
        Enum(AsyncJobStatus, name="async_job_status", create_type=False),
        nullable=False,
        server_default=text("'pending'"),
    )
    payload_review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dish_reviews.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
