"""add is_anonymous to dish_reviews

Revision ID: 007
Revises: 006
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dish_reviews",
        sa.Column(
            "is_anonymous",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("dish_reviews", "is_anonymous")
