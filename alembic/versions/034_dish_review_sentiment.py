"""Sentiment label + score per dish review.

Adds three columns to ``dish_reviews`` so the owner dashboard and the
Business chatbot can triage which reviews to respond to first:

- ``sentiment_label`` — coarse bucket (positive / neutral / negative).
- ``sentiment_score`` — fine-grained score in [-1.00, 1.00] for sorting.
- ``sentiment_analyzed_at`` — when the LLM produced the values.

All nullable. New reviews get filled async after creation; historical
ones get filled by ``app.scripts.backfill_sentiment``. A partial index
makes ``WHERE sentiment_label = '...'`` cheap without bloating every
row in the table.

Revision ID: 034
Revises: 033
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum


revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


sentiment_label_t = PGEnum(
    "positive", "neutral", "negative",
    name="sentiment_label", create_type=False,
)


def upgrade() -> None:
    # Idempotent enum creation: a previous failed run might have left
    # the type behind. Same pattern as 031_chat_agentic_foundation.
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE sentiment_label AS ENUM (
                'positive', 'neutral', 'negative'
            );
        EXCEPTION WHEN duplicate_object THEN
            null;
        END $$;
        """
    )

    op.add_column(
        "dish_reviews",
        sa.Column("sentiment_label", sentiment_label_t, nullable=True),
    )
    op.add_column(
        "dish_reviews",
        sa.Column("sentiment_score", sa.Numeric(3, 2), nullable=True),
    )
    op.add_column(
        "dish_reviews",
        sa.Column(
            "sentiment_analyzed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_check_constraint(
        "ck_dish_reviews_sentiment_score_range",
        "dish_reviews",
        "sentiment_score IS NULL "
        "OR (sentiment_score >= -1 AND sentiment_score <= 1)",
    )

    # Partial index — only indexes rows with sentiment computed. Reads
    # for the owner dashboard / chatbot filter on
    # ``sentiment_label = '...'`` and never on NULL.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dish_reviews_sentiment_label "
        "ON dish_reviews (sentiment_label) "
        "WHERE sentiment_label IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dish_reviews_sentiment_label")
    op.drop_constraint(
        "ck_dish_reviews_sentiment_score_range",
        "dish_reviews",
        type_="check",
    )
    op.drop_column("dish_reviews", "sentiment_analyzed_at")
    op.drop_column("dish_reviews", "sentiment_score")
    op.drop_column("dish_reviews", "sentiment_label")
    op.execute("DROP TYPE IF EXISTS sentiment_label")
