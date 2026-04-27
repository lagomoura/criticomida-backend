"""add Google Places enrichment fields

Revision ID: 014
Revises: 013
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("google_rating", sa.Numeric(2, 1), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column("google_user_ratings_total", sa.Integer(), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column("google_photos", JSONB(), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column("editorial_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column("editorial_summary_lang", sa.String(8), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column(
            "cuisine_types",
            sa.ARRAY(sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "restaurants",
        sa.Column(
            "google_cached_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("restaurants", "google_cached_at")
    op.drop_column("restaurants", "cuisine_types")
    op.drop_column("restaurants", "editorial_summary_lang")
    op.drop_column("restaurants", "editorial_summary")
    op.drop_column("restaurants", "google_photos")
    op.drop_column("restaurants", "google_user_ratings_total")
    op.drop_column("restaurants", "google_rating")
