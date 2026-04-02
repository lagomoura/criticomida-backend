"""make restaurant category_id nullable

Revision ID: 004
Revises: 003_citext_checks_user_feedback
Create Date: 2026-04-02

The category of a restaurant is derived from its dishes, not assigned at
creation time. Making category_id nullable allows restaurants to exist
without a category until their dish reviews establish one.
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "restaurants",
        "category_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Rows with NULL category_id must be handled before downgrading.
    # Set them to category 1 (Dulces) as a safe fallback.
    op.execute(
        "UPDATE restaurants SET category_id = 1 WHERE category_id IS NULL"
    )
    op.alter_column(
        "restaurants",
        "category_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
