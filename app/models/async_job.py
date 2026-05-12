"""Persistent queue for write-behind jobs.

The queue is consumed by a single worker loop launched in
``app.main.production_lifespan`` (see ``app/services/async_job_worker.py``).

Job kinds today, by payload shape:

- ``embed_review`` / ``sentiment_review`` — keyed off a ``DishReview``.
  Original kinds; payload is ``payload_review_id``.
- ``sommelier_review_recall`` — keyed off ``(user_id, dish_id)``.
  Enqueued from the ``recommend_dishes`` tool with a delayed
  ``scheduled_at`` (default 24h). When picked up, drops an in-app
  notification reminding the diner to review the dish — unless they
  already did, the actor is blocked/muted, or a notification for
  the same (user, dish) already exists.

The payload columns are typed siblings rather than a generic JSONB
blob: FKs let Postgres cascade-delete pending jobs when the user or
dish disappears, and a typed column is cheaper to index than a JSON
path. ``ck_async_job_payload_shape`` (migration 063) keeps each row
coherent — exactly one of the payload shapes is populated per row.
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
    sommelier_review_recall = "sommelier_review_recall"


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
    # Payload variant 1: review-keyed jobs (embed_review, sentiment_review).
    payload_review_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dish_reviews.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Payload variant 2: (user, dish)-keyed jobs (sommelier_review_recall).
    payload_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    payload_dish_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dishes.id", ondelete="CASCADE"),
        nullable=True,
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
