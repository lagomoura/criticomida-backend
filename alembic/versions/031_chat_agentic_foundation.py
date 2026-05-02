"""Phase 0 of the agentic chatbot rewrite.

- Enables the ``vector`` extension (pgvector) for semantic search.
- Creates ``chat_conversations`` and ``chat_messages`` so multi-turn
  conversations + tool calls survive across sessions.
- Creates ``user_taste_profiles`` to inject a personalized snapshot in
  the system prompt (greet by name, reason about likes).
- Creates ``dish_review_embeddings`` and ``dish_embeddings`` (768 dims
  for Gemini ``text-embedding-004``) with HNSW indexes for KNN.

Revision ID: 031
Revises: 030
Create Date: 2026-05-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMBEDDING_DIMENSIONS = 768


# Idempotent ENUM creation: a previous failed run of this migration may
# have left the type behind. ``DO`` block swallows ``duplicate_object``
# so re-runs after a partial apply succeed.
def _create_enum_if_missing(name: str, *values: str) -> None:
    quoted = ", ".join(f"'{v}'" for v in values)
    op.execute(
        f"""
        DO $$ BEGIN
            CREATE TYPE {name} AS ENUM ({quoted});
        EXCEPTION WHEN duplicate_object THEN
            null;
        END $$;
        """
    )


# Reusable column types. ``create_type=False`` because the DO block above
# is the only place we touch the type definition — the column refs just
# point at it by name.
chat_agent_t = PGEnum(
    "sommelier", "ghostwriter", "business",
    name="chat_agent", create_type=False,
)
taste_pillar_t = PGEnum(
    "presentation", "execution", "value_prop",
    name="taste_pillar", create_type=False,
)
price_band_t = PGEnum(
    "low", "mid", "high", name="price_band", create_type=False,
)


def upgrade() -> None:
    # ── pgvector extension ─────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── enums (idempotent) ─────────────────────────────────────────────────
    _create_enum_if_missing(
        "chat_agent", "sommelier", "ghostwriter", "business"
    )
    _create_enum_if_missing(
        "taste_pillar", "presentation", "execution", "value_prop"
    )
    _create_enum_if_missing("price_band", "low", "mid", "high")

    # ── chat_conversations ─────────────────────────────────────────────────
    op.create_table(
        "chat_conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "agent",
            chat_agent_t,
            nullable=False,
            server_default="sommelier",
        ),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column(
            "restaurant_scope_id",
            UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_chat_conversations_user_id",
        "chat_conversations",
        ["user_id"],
    )
    op.create_index(
        "ix_chat_conversations_user_last",
        "chat_conversations",
        ["user_id", "last_message_at"],
    )

    # ── chat_messages ──────────────────────────────────────────────────────
    op.create_table(
        "chat_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls", JSONB, nullable=True),
        sa.Column("tool_result", JSONB, nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant','tool')",
            name="ck_chat_messages_role",
        ),
    )
    op.create_index(
        "ix_chat_messages_conversation_created",
        "chat_messages",
        ["conversation_id", "created_at"],
    )

    # ── user_taste_profiles ────────────────────────────────────────────────
    op.create_table(
        "user_taste_profiles",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("dominant_pillar", taste_pillar_t, nullable=True),
        sa.Column(
            "top_neighborhoods",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "top_categories",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("avg_price_band", price_band_t, nullable=True),
        sa.Column(
            "favorite_tags",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "preferred_hours",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "allergies",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "review_count_at_last_compute",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── dish_review_embeddings ─────────────────────────────────────────────
    op.create_table(
        "dish_review_embeddings",
        sa.Column(
            "dish_review_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "embedding",
            Vector(EMBEDDING_DIMENSIONS),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # HNSW index for cosine distance (Gemini embeddings are normalized).
    op.execute(
        "CREATE INDEX ix_dish_review_embeddings_hnsw "
        "ON dish_review_embeddings USING hnsw (embedding vector_cosine_ops)"
    )

    # ── dish_embeddings ────────────────────────────────────────────────────
    op.create_table(
        "dish_embeddings",
        sa.Column(
            "dish_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dishes.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "embedding",
            Vector(EMBEDDING_DIMENSIONS),
            nullable=False,
        ),
        sa.Column("source_text_hash", sa.String(64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_dish_embeddings_hnsw "
        "ON dish_embeddings USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dish_embeddings_hnsw")
    op.drop_table("dish_embeddings")

    op.execute("DROP INDEX IF EXISTS ix_dish_review_embeddings_hnsw")
    op.drop_table("dish_review_embeddings")

    op.drop_table("user_taste_profiles")

    op.drop_index(
        "ix_chat_messages_conversation_created", table_name="chat_messages"
    )
    op.drop_table("chat_messages")

    op.drop_index(
        "ix_chat_conversations_user_last", table_name="chat_conversations"
    )
    op.drop_index(
        "ix_chat_conversations_user_id", table_name="chat_conversations"
    )
    op.drop_table("chat_conversations")

    op.execute("DROP TYPE IF EXISTS price_band")
    op.execute("DROP TYPE IF EXISTS taste_pillar")
    op.execute("DROP TYPE IF EXISTS chat_agent")
    # Vector extension is left in place: other future migrations may use it.
