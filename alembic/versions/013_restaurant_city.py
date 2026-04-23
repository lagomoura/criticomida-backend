"""Add restaurants.city for trending queries.

Revision ID: 013
Revises: 012
Create Date: 2026-04-23

Nullable column; no backfill. Populated going forward by the compose flow,
which reads `city` from Google Places `address_components.locality`. Existing
85 rows stay null and simply don't appear in trending queries until they're
re-reviewed through the Places path.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("restaurants", sa.Column("city", sa.String(length=100), nullable=True))
    op.create_index("ix_restaurants_city", "restaurants", ["city", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_restaurants_city", table_name="restaurants")
    op.drop_column("restaurants", "city")
