"""add editorial enrichment fields to dishes

Revision ID: 015
Revises: 014
Create Date: 2026-04-27
"""

from alembic import op
import sqlalchemy as sa


revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dishes",
        sa.Column("editorial_blurb", sa.Text(), nullable=True),
    )
    op.add_column(
        "dishes",
        sa.Column("editorial_blurb_lang", sa.String(8), nullable=True),
    )
    op.add_column(
        "dishes",
        sa.Column("editorial_blurb_source", sa.String(20), nullable=True),
    )
    op.add_column(
        "dishes",
        sa.Column(
            "editorial_cached_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dishes", "editorial_cached_at")
    op.drop_column("dishes", "editorial_blurb_source")
    op.drop_column("dishes", "editorial_blurb_lang")
    op.drop_column("dishes", "editorial_blurb")
