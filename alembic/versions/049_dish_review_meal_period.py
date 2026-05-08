"""Meal period (breakfast/lunch/snack/dinner) on dish reviews.

Replaces the old free-form ``time_tasted`` input flow on the frontend
with a 4-way preset picker. The exact time column stays in place for
compatibility with legacy reviews, but new writes go through this enum.

Revision ID: 049
Revises: 048
Create Date: 2026-05-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum


revision: str = "049"
down_revision: Union[str, None] = "048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


meal_period_t = PGEnum(
    "breakfast", "lunch", "snack", "dinner",
    name="meal_period", create_type=False,
)


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE meal_period AS ENUM (
                'breakfast', 'lunch', 'snack', 'dinner'
            );
        EXCEPTION WHEN duplicate_object THEN
            null;
        END $$;
        """
    )

    op.add_column(
        "dish_reviews",
        sa.Column("meal_period", meal_period_t, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dish_reviews", "meal_period")
    op.execute("DROP TYPE IF EXISTS meal_period")
