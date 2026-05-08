"""Persistent async-job queue for embedding/sentiment write-behind.

Replaces the ``asyncio.create_task`` fire-and-forget pattern in
``embeddings_service.schedule_reembed_review`` and
``sentiment_service.schedule_analyze_review``. Those tasks were lost
on every Railway redeploy (SIGTERM kills in-flight coroutines) and
the affected reviews ended up without an embedding / sentiment row,
silently degrading semantic search for the Sommelier.

Design notes:

- Single table ``async_job`` with a ``kind`` enum so a future job
  type (e.g. ``backfill_taste_profile``) can land without another
  migration. The enum and the status enum follow the canonical
  idempotent pattern from migration 031 (``DO $$ ... duplicate_object``).
- ``payload_review_id`` is the only payload column today. We keep
  it explicit (typed UUID FK with ``ON DELETE CASCADE``) instead of
  a generic JSONB blob so foreign-key consistency is enforced and
  a deleted review automatically removes its pending jobs.
- ``ix_async_job_pending``: partial index on
  ``(kind, scheduled_at)`` filtered to ``status IN ('pending', 'running')``.
  The worker picks the next job with this index without scanning
  ``done`` / ``failed`` rows that accumulate over time.
- ``ix_async_job_pending_dedup``: partial UNIQUE index on
  ``(kind, payload_review_id)`` filtered to ``status = 'pending'``.
  Lets the writer ``ON CONFLICT DO NOTHING`` when a review is edited
  twice in quick succession — second edit collapses into the first
  pending row instead of double-running the LLM.

Revision ID: 053
Revises: 052
Create Date: 2026-05-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID


revision: str = "053"
down_revision: Union[str, None] = "052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


# create_type=False because the DO block above is the single source
# of truth for the type definition.
async_job_kind_t = PGEnum(
    "embed_review",
    "sentiment_review",
    name="async_job_kind",
    create_type=False,
)
async_job_status_t = PGEnum(
    "pending",
    "running",
    "done",
    "failed",
    name="async_job_status",
    create_type=False,
)


def upgrade() -> None:
    _create_enum_if_missing(
        "async_job_kind", "embed_review", "sentiment_review"
    )
    _create_enum_if_missing(
        "async_job_status", "pending", "running", "done", "failed"
    )

    op.create_table(
        "async_job",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", async_job_kind_t, nullable=False),
        sa.Column(
            "status",
            async_job_status_t,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "payload_review_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Worker pickup index: only scans rows the worker still cares about.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_async_job_pending
            ON async_job (kind, scheduled_at)
            WHERE status IN ('pending', 'running');
        """
    )

    # Dedup: at most one pending job per (kind, review). The writer can
    # ``ON CONFLICT DO NOTHING`` and a quick second edit folds into the
    # already-pending one.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_async_job_pending_dedup
            ON async_job (kind, payload_review_id)
            WHERE status = 'pending';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_async_job_pending_dedup;")
    op.execute("DROP INDEX IF EXISTS ix_async_job_pending;")
    op.drop_table("async_job")
    op.execute("DROP TYPE IF EXISTS async_job_status;")
    op.execute("DROP TYPE IF EXISTS async_job_kind;")
