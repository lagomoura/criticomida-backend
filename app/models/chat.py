"""Models for the agentic chatbot stack.

Phase 0 introduces five tables:

- ``chat_conversations`` — one row per opened conversation. Anonymous users
  can chat too (``user_id`` nullable). The ``agent`` column lets the same
  storage hold Sommelier (B2C), Ghostwriter (B2C) and Business (B2B) chats.
- ``chat_messages`` — full transcript with tool calls/results stored as
  JSONB so the orchestrator can replay multi-turn tool loops verbatim.
- ``user_taste_profiles`` — structured snapshot of the user's preferences,
  refreshed asynchronously after they rate dishes. Injected in the system
  prompt so the bot can greet personally and reason about likes.
- ``dish_review_embeddings`` / ``dish_embeddings`` — pgvector storage for
  Gemini ``text-embedding-004`` (768 dims). Used for semantic re-ranking
  *after* the LLM has applied structured filters (neighborhood, pillars).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


EMBEDDING_DIMENSIONS = 768  # gemini text-embedding-004


class ChatAgent(str, enum.Enum):
    sommelier = "sommelier"
    ghostwriter = "ghostwriter"
    business = "business"


class TastePillar(str, enum.Enum):
    presentation = "presentation"
    execution = "execution"
    value_prop = "value_prop"


class PriceBand(str, enum.Enum):
    low = "low"
    mid = "mid"
    high = "high"


class ChatConversation(Base):
    """A single chat session. ``user_id`` is nullable to allow anonymous
    chats; in that case the conversation is ephemeral (no taste profile
    injected, no recall across sessions).

    ``restaurant_scope_id`` only carries a value for Business agent
    sessions opened from the owner dashboard — it pins every tool call to
    the owner's restaurant so leakage across owners is impossible at the
    data layer.
    """

    __tablename__ = "chat_conversations"
    __table_args__ = (
        Index("ix_chat_conversations_user_last", "user_id", "last_message_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent: Mapped[ChatAgent] = mapped_column(
        Enum(ChatAgent, name="chat_agent"),
        nullable=False,
        default=ChatAgent.sommelier,
        server_default=ChatAgent.sommelier.value,
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    restaurant_scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class ChatMessage(Base):
    """One message in a conversation.

    ``role='tool'`` rows store the tool *result* paired with the
    assistant's original ``tool_calls`` (kept on the previous assistant
    row). Persisting both halves lets us replay a session deterministically
    and lets Business audits review what the agent actually did.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','tool')",
            name="ck_chat_messages_role",
        ),
        Index(
            "ix_chat_messages_conversation_created",
            "conversation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Anthropic tool_use blocks emitted by the assistant (jsonb list).
    tool_calls: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    # tool_use_id + result body for role='tool' rows.
    tool_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Per-message token accounting for cost monitoring.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UserTasteProfile(Base):
    """Aggregated, machine-derived snapshot of a user's tastes.

    Allergies are *not* inferable: that field is only populated by an
    explicit declaration from the user (via the chat tool
    ``update_taste_profile`` or a profile screen). Everything else is
    recomputed by ``taste_profile_service`` on dish-review writes.

    ``version`` lets us bump the heuristic without a migration: a service
    bump invalidates older rows without dropping data.
    """

    __tablename__ = "user_taste_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dominant_pillar: Mapped[TastePillar | None] = mapped_column(
        Enum(TastePillar, name="taste_pillar"), nullable=True
    )
    top_neighborhoods: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    top_categories: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    avg_price_band: Mapped[PriceBand | None] = mapped_column(
        Enum(PriceBand, name="price_band"), nullable=True
    )
    favorite_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    preferred_hours: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # User-declared, NEVER inferred. Free-form strings.
    allergies: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    review_count_at_last_compute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class DishReviewEmbedding(Base):
    """One vector per dish review. The text embedded is the review note +
    pros/cons + tags concatenated. Recomputed on review create/update.
    """

    __tablename__ = "dish_review_embeddings"

    dish_review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dish_reviews.id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class DishEmbedding(Base):
    """One aggregated vector per dish. Built from dish name + description +
    editorial blurb + a digest of top reviews. The hash lets us skip
    recompute when nothing changed.
    """

    __tablename__ = "dish_embeddings"

    dish_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dishes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    embedding: Mapped[list[float]] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=False
    )
    source_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
