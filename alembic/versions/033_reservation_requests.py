"""Phase 3 of the agentic chatbot: reservation requests + new
notification kind ``reservation_requested`` for owner alerts.

Revision ID: 033
Revises: 032
Create Date: 2026-05-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID


revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── reservation_status enum (idempotent) ───────────────────────────────
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE reservation_status AS ENUM (
                'pending', 'accepted', 'rejected', 'cancelled'
            );
        EXCEPTION WHEN duplicate_object THEN
            null;
        END $$;
        """
    )

    reservation_status_t = PGEnum(
        "pending", "accepted", "rejected", "cancelled",
        name="reservation_status", create_type=False,
    )

    # ── reservation_requests ───────────────────────────────────────────────
    op.create_table(
        "reservation_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "requester_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "restaurant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("party_size", sa.SmallInteger(), nullable=False),
        sa.Column("requested_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "status",
            reservation_status_t,
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "source_conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "party_size >= 1 AND party_size <= 30",
            name="ck_reservation_requests_party_size_range",
        ),
    )
    op.create_index(
        "ix_reservation_requests_owner_status",
        "reservation_requests",
        ["owner_user_id", "status", "requested_for"],
    )
    op.create_index(
        "ix_reservation_requests_user_created",
        "reservation_requests",
        ["requester_user_id", "created_at"],
    )

    # ── extend notifications.kind ──────────────────────────────────────────
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN ('like','comment','follow','claim_approved',"
        "'claim_rejected','claim_revoked','comment_like','comment_reply',"
        "'reservation_requested')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN ('like','comment','follow','claim_approved',"
        "'claim_rejected','claim_revoked','comment_like','comment_reply')",
    )

    op.drop_index(
        "ix_reservation_requests_user_created", table_name="reservation_requests"
    )
    op.drop_index(
        "ix_reservation_requests_owner_status", table_name="reservation_requests"
    )
    op.drop_table("reservation_requests")
    op.execute("DROP TYPE IF EXISTS reservation_status")
