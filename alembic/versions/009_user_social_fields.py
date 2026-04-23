"""Add user social fields: handle, bio, location.

Revision ID: 009
Revises: 008
Create Date: 2026-04-22

Adds the minimum User surface needed by the social product spec:
- handle: case-insensitive, URL-safe, unique when present.
- bio: short text describing the user.
- location: free-text location.

All three are nullable so existing rows require no backfill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import CITEXT


revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("handle", CITEXT(), nullable=True))
    op.add_column("users", sa.Column("bio", sa.String(length=500), nullable=True))
    op.add_column("users", sa.Column("location", sa.String(length=200), nullable=True))

    # Unique index that allows multiple NULLs (default Postgres behavior on
    # simple unique indexes allows nulls, but we use a partial predicate to
    # make the intent explicit and to avoid colliding with any global unique
    # tooling assumptions).
    op.create_index(
        "ux_users_handle",
        "users",
        ["handle"],
        unique=True,
        postgresql_where=sa.text("handle IS NOT NULL"),
    )

    op.create_check_constraint(
        "ck_users_handle_format",
        "users",
        "handle IS NULL OR handle ~ '^[a-z0-9_]{3,30}$'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_handle_format", "users", type_="check")
    op.drop_index("ux_users_handle", table_name="users")
    op.drop_column("users", "location")
    op.drop_column("users", "bio")
    op.drop_column("users", "handle")
