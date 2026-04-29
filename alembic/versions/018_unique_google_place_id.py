"""unique partial index on restaurants.google_place_id to dedupe Google Places entries

Revision ID: 018
Revises: 017
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_restaurants_google_place_id",
        "restaurants",
        ["google_place_id"],
        unique=True,
        postgresql_where=sa.text("google_place_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_restaurants_google_place_id", table_name="restaurants")
