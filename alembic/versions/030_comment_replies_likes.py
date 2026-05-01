"""Replies anidadas (1 nivel) + likes en comentarios.

- comments.parent_comment_id (FK self, cascade) para respuestas.
- comment_likes (PK compuesta user_id+comment_id) espejando likes.
- notifications.target_comment_id + nuevos kinds 'comment_like' y
  'comment_reply'.
- ix_comments_review_created se rearma para listar sólo top-level
  (parent_comment_id IS NULL AND removed_at IS NULL).

Revision ID: 030
Revises: 029
Create Date: 2026-05-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── comments.parent_comment_id ─────────────────────────────────────────
    op.add_column(
        "comments",
        sa.Column(
            "parent_comment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("comments.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_comments_parent_created",
        "comments",
        ["parent_comment_id", "created_at"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )

    # Rebuild the top-level listing index so it skips replies.
    # IF EXISTS porque algunos snapshots de dev no traen el índice histórico.
    op.execute("DROP INDEX IF EXISTS ix_comments_review_created")
    op.create_index(
        "ix_comments_review_created",
        "comments",
        ["review_id", "created_at"],
        postgresql_where=sa.text(
            "removed_at IS NULL AND parent_comment_id IS NULL"
        ),
    )

    # ── comment_likes ──────────────────────────────────────────────────────
    op.create_table(
        "comment_likes",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "comment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("comments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "comment_id", name="pk_comment_likes"),
    )
    op.create_index(
        "ix_comment_likes_comment",
        "comment_likes",
        ["comment_id"],
    )

    # ── notifications.target_comment_id + nuevos kinds ─────────────────────
    op.add_column(
        "notifications",
        sa.Column(
            "target_comment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("comments.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN ('like','comment','follow','claim_approved',"
        "'claim_rejected','claim_revoked','comment_like','comment_reply')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        "kind IN ('like','comment','follow','claim_approved',"
        "'claim_rejected','claim_revoked')",
    )
    op.drop_column("notifications", "target_comment_id")

    op.drop_index("ix_comment_likes_comment", table_name="comment_likes")
    op.drop_table("comment_likes")

    op.execute("DROP INDEX IF EXISTS ix_comments_review_created")
    op.create_index(
        "ix_comments_review_created",
        "comments",
        ["review_id", "created_at"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.execute("DROP INDEX IF EXISTS ix_comments_parent_created")
    op.drop_column("comments", "parent_comment_id")
