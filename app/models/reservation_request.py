"""Reservation requests created by the Sommelier chatbot.

When a logged-in user asks the bot to "reservame una mesa" the
``request_reservation`` tool drops a row here and, if the restaurant
has a verified owner, raises a notification + Resend email so the
owner can act on it from their dashboard.

If the restaurant has no claimed owner, the bot doesn't write a row at
all — it falls back to opening ``Restaurant.reservation_url`` (the
existing partner deeplink). That decision lives at the tool layer; this
table only tracks requests where there's actually somebody to handle
them.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReservationStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    cancelled = "cancelled"


class ReservationRequest(Base):
    """One reservation request raised by a user via the chatbot."""

    __tablename__ = "reservation_requests"
    __table_args__ = (
        CheckConstraint(
            "party_size >= 1 AND party_size <= 30",
            name="ck_reservation_requests_party_size_range",
        ),
        Index(
            "ix_reservation_requests_owner_status",
            "owner_user_id",
            "status",
            "requested_for",
        ),
        Index(
            "ix_reservation_requests_user_created",
            "requester_user_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Snapshot of who owned the restaurant at request time. Persists even
    # if the claim is later revoked, so audit trails stay intact.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    party_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    requested_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, name="reservation_status"),
        nullable=False,
        default=ReservationStatus.pending,
        server_default=ReservationStatus.pending.value,
    )
    # Optional source: id of the chat conversation that spawned the
    # request. Useful when the owner wants to read the back-and-forth.
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
