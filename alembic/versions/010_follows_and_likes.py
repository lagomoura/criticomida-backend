"""Follows and likes tables.

Revision ID: 010
Revises: 009
Create Date: 2026-04-22

Adds the two social primitives required by PR 2:
- follows: asymmetric (follower -> following) with composite PK.
- likes: user -> dish review with composite PK.

No data backfill required.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follows",
        sa.Column(
            "follower_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "following_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("follower_id", "following_id", name="pk_follows"),
        sa.CheckConstraint(
            "follower_id <> following_id",
            name="ck_follows_no_self",
        ),
    )
    op.create_index(
        "ix_follows_following_id",
        "follows",
        ["following_id", "created_at"],
    )

    op.create_table(
        "likes",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "review_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "review_id", name="pk_likes"),
    )
    op.create_index(
        "ix_likes_review_id",
        "likes",
        ["review_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_likes_review_id", table_name="likes")
    op.drop_table("likes")
    op.drop_index("ix_follows_following_id", table_name="follows")
    op.drop_table("follows")
