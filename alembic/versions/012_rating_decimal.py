"""Change dish_reviews.rating from int to NUMERIC(2,1).

Revision ID: 012
Revises: 011
Create Date: 2026-04-23

Social UI expects half-step ratings (e.g. 3.5). Existing int values are
lossless when cast to numeric(2,1) — `4` becomes `4.0`. Existing CHECK
`rating >= 1 AND rating <= 5` keeps holding without edits.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "dish_reviews",
        "rating",
        existing_type=sa.Integer(),
        type_=sa.Numeric(2, 1),
        existing_nullable=False,
        postgresql_using="rating::numeric(2,1)",
    )


def downgrade() -> None:
    op.alter_column(
        "dish_reviews",
        "rating",
        existing_type=sa.Numeric(2, 1),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="rating::integer",
    )
