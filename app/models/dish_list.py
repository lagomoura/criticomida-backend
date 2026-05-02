"""Curated lists of dishes ("rutas").

The Sommelier creates these via the ``create_dish_route`` tool when the
user asks for things like "armame una ruta de 3 platos ganadores en el
centro". Lists are also editable from the user's profile in later
phases.

Two tables:

- ``dish_lists`` — one row per list, with a unique slug for public
  sharing. ``is_public=true`` makes the list reachable at
  ``/listas/{slug}`` even for unauthenticated visitors.
- ``dish_list_items`` — composite PK on ``(list_id, dish_id)`` so a
  dish can't appear twice in the same list. ``position`` drives the
  visual order; the chat tool fills it sequentially as items come in.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DishList(Base):
    __tablename__ = "dish_lists"
    __table_args__ = (
        Index("ix_dish_lists_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(
        String(120), unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ``true`` allows anonymous visitors to read the list at /listas/{slug}.
    # ``false`` keeps it private to the owner.
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # When the bot creates a list, we keep the conversation that
    # spawned it so the user can return to that chat later. Optional.
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

    items: Mapped[list["DishListItem"]] = relationship(
        back_populates="dish_list",
        cascade="all, delete-orphan",
        order_by="DishListItem.position",
    )


class DishListItem(Base):
    __tablename__ = "dish_list_items"
    __table_args__ = (
        PrimaryKeyConstraint(
            "list_id", "dish_id", name="pk_dish_list_items"
        ),
        Index("ix_dish_list_items_list_position", "list_id", "position"),
    )

    list_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dish_lists.id", ondelete="CASCADE"),
        nullable=False,
    )
    dish_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dishes.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    dish_list: Mapped["DishList"] = relationship(back_populates="items")
