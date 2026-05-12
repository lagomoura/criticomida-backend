"""063 — Sommelier Review Recall scaffolding.

D2 feature: 24h after the Sommelier recommends a dish, if the diner
hasn't reviewed it yet, we drop an in-app notification suggesting
they write the review. The recall is processed by the existing
async_job worker so we ride the same retry/dedup machinery as
embedding/sentiment jobs — no external cron needed.

This migration introduces:

- ``sommelier_review_recall`` value in ``async_job_kind`` enum.
- ``payload_user_id`` and ``payload_dish_id`` columns on async_job;
  ``payload_review_id`` becomes nullable (the recall job is keyed
  off (user, dish), not a review). A CHECK keeps each row coherent:
  either review-keyed (original kinds) or user+dish-keyed (recall).
- Partial UNIQUE index on (kind, payload_user_id, payload_dish_id)
  filtered to pending recall rows — bounds the queue size if the
  agent re-recommends the same dish before the first recall fires.
- ``sommelier_review_recall`` value in the ``notifications.kind``
  CHECK constraint.
- ``target_dish_id`` column on notifications (FK CASCADE) — the
  recall surfaces a single dish, the link points at the compose
  form pre-filled with that dish.
- ``sommelier`` bot user with a deterministic UUID so the
  notification has a sensible actor. Email/password/handle are
  placeholders that won't collide with real users: the email lives
  at the brand domain, the password_hash is an invalid bcrypt
  prefix so no login flow can ever match it.

Revision ID: 063
Revises: 062
Create Date: 2026-05-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "063"
down_revision: Union[str, None] = "062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Deterministic UUID. Last 12 hex chars = ASCII "Palato"
# (50=P 61=a 6c=l 61=a 74=t 6f=o). v4-shaped (version=4, variant=8)
# so any UUID validator that checks bits stays happy.
_SOMMELIER_BOT_USER_ID = "00000000-0000-4000-8000-50616c61746f"


_KINDS_OLD = (
    "like",
    "comment",
    "follow",
    "claim_approved",
    "claim_rejected",
    "claim_revoked",
    "comment_like",
    "comment_reply",
    "reservation_requested",
    "review_on_owned_restaurant",
    "mention",
)
_KINDS_NEW = _KINDS_OLD + ("sommelier_review_recall",)


def _ck_kind_clause(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
    # ──────────────────────────────────────────────────────────────────
    # 1. Extend async_job_kind enum. Postgres refuses to use a new
    #    enum value in the same transaction that created it
    #    (``UnsafeNewEnumValueUsageError``) — and the CHECK below
    #    references the new value. ``autocommit_block`` commits the
    #    ALTER TYPE on its own tx so the rest of the migration can
    #    name the value freely. ``IF NOT EXISTS`` keeps re-runs safe
    #    against the CI matrix that re-applies migrations.
    # ──────────────────────────────────────────────────────────────────
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE async_job_kind "
            "ADD VALUE IF NOT EXISTS 'sommelier_review_recall'"
        )

    # ──────────────────────────────────────────────────────────────────
    # 2. async_job: make payload_review_id nullable and add the new
    #    (user_id, dish_id) sibling columns for the recall kind.
    # ──────────────────────────────────────────────────────────────────
    op.alter_column(
        "async_job",
        "payload_review_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "async_job",
        sa.Column(
            "payload_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "async_job",
        sa.Column(
            "payload_dish_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dishes.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # Coherence guard: each row carries either the review-keyed payload
    # (old kinds) OR the user+dish-keyed payload (recall). Both columns
    # null or both kinds of payload populated would be a programming
    # error; the CHECK turns it into a constraint violation we can
    # surface, instead of silently producing a no-op job.
    op.create_check_constraint(
        "ck_async_job_payload_shape",
        "async_job",
        (
            "(kind = 'sommelier_review_recall' AND payload_user_id IS NOT NULL "
            "AND payload_dish_id IS NOT NULL AND payload_review_id IS NULL) "
            "OR (kind IN ('embed_review', 'sentiment_review') "
            "AND payload_review_id IS NOT NULL "
            "AND payload_user_id IS NULL AND payload_dish_id IS NULL)"
        ),
    )

    # Dedup for recall rows: at most one pending recall per (user, dish).
    # The handler will also defend against post-fired duplicates via a
    # notification-side check, but this short-circuits the enqueue path.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_async_job_pending_recall_dedup
            ON async_job (kind, payload_user_id, payload_dish_id)
            WHERE status = 'pending' AND kind = 'sommelier_review_recall';
        """
    )

    # ──────────────────────────────────────────────────────────────────
    # 3. notifications: new kind value + target_dish_id column.
    # ──────────────────────────────────────────────────────────────────
    op.drop_constraint(
        "ck_notifications_kind", "notifications", type_="check"
    )
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        _ck_kind_clause(_KINDS_NEW),
    )
    op.add_column(
        "notifications",
        sa.Column(
            "target_dish_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dishes.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # Dedup-aid index: the recall handler queries
    # (recipient_user_id, kind, target_dish_id) to confirm it hasn't
    # already notified this user about this dish. Filtered to the recall
    # kind to keep the index narrow.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_notifications_recall_dedup
            ON notifications (recipient_user_id, target_dish_id)
            WHERE kind = 'sommelier_review_recall';
        """
    )

    # ──────────────────────────────────────────────────────────────────
    # 4. Insert the Sommelier bot user. Placeholder credentials:
    #    - email at the brand domain so it stays out of the real
    #      address space.
    #    - password_hash starts with '!' — bcrypt/argon2 hashes never
    #      start with that character, so the login verifier rejects it
    #      structurally without us having to maintain a blocklist.
    #    - email_verified_at set to now() so the row passes any
    #      "verified-only" checks downstream.
    # ──────────────────────────────────────────────────────────────────
    op.execute(
        f"""
        INSERT INTO users (
            id, email, password_hash, display_name, handle,
            role, created_at, updated_at, email_verified_at
        ) VALUES (
            '{_SOMMELIER_BOT_USER_ID}',
            'sommelier-bot@palato.me',
            '!system-no-login!',
            'Sommelier',
            'sommelier',
            'user',
            now(), now(), now()
        )
        ON CONFLICT DO NOTHING;
        """
    )


def downgrade() -> None:
    # The bot user might still own notifications inserted during the
    # window the feature was live. CASCADE on notifications.actor_user_id
    # would wipe them automatically, so the delete is enough — no need
    # to clean up notifications by hand.
    op.execute(
        f"DELETE FROM users WHERE id = '{_SOMMELIER_BOT_USER_ID}';"
    )

    op.execute("DROP INDEX IF EXISTS ix_notifications_recall_dedup;")
    op.drop_column("notifications", "target_dish_id")
    op.drop_constraint(
        "ck_notifications_kind", "notifications", type_="check"
    )
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        _ck_kind_clause(_KINDS_OLD),
    )

    op.execute("DROP INDEX IF EXISTS ix_async_job_pending_recall_dedup;")
    op.drop_constraint(
        "ck_async_job_payload_shape", "async_job", type_="check"
    )
    op.drop_column("async_job", "payload_dish_id")
    op.drop_column("async_job", "payload_user_id")
    op.alter_column(
        "async_job",
        "payload_review_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
    # Note: ``ALTER TYPE ... DROP VALUE`` is not supported by Postgres
    # without rebuilding the enum from scratch, and any historical
    # rows referencing 'sommelier_review_recall' would block it. The
    # value stays in the enum on downgrade — harmless if no code
    # references it.
