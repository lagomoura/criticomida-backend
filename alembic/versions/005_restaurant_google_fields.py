"""add Google Maps fields to restaurants

Revision ID: 005
Revises: 004
Create Date: 2026-04-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("restaurants", sa.Column("google_place_id", sa.String(200), nullable=True))
    op.add_column("restaurants", sa.Column("website", sa.String(500), nullable=True))
    op.add_column("restaurants", sa.Column("phone_number", sa.String(50), nullable=True))
    op.add_column("restaurants", sa.Column("google_maps_url", sa.String(500), nullable=True))
    op.add_column("restaurants", sa.Column("price_level", sa.SmallInteger(), nullable=True))
    op.add_column("restaurants", sa.Column("opening_hours", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("restaurants", "opening_hours")
    op.drop_column("restaurants", "price_level")
    op.drop_column("restaurants", "google_maps_url")
    op.drop_column("restaurants", "phone_number")
    op.drop_column("restaurants", "website")
    op.drop_column("restaurants", "google_place_id")
