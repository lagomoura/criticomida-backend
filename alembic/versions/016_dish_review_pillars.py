"""add technical pillars (presentation, value_prop, execution) to dish_reviews

Revision ID: 016
Revises: 015
Create Date: 2026-04-28
"""

from alembic import op
import sqlalchemy as sa


revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dish_reviews",
        sa.Column("presentation", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "dish_reviews",
        sa.Column("value_prop", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "dish_reviews",
        sa.Column("execution", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_dish_reviews_presentation_range",
        "dish_reviews",
        "presentation IS NULL OR presentation BETWEEN 1 AND 3",
    )
    op.create_check_constraint(
        "ck_dish_reviews_value_prop_range",
        "dish_reviews",
        "value_prop IS NULL OR value_prop BETWEEN 1 AND 3",
    )
    op.create_check_constraint(
        "ck_dish_reviews_execution_range",
        "dish_reviews",
        "execution IS NULL OR execution BETWEEN 1 AND 3",
    )


def downgrade() -> None:
    op.drop_constraint("ck_dish_reviews_execution_range", "dish_reviews", type_="check")
    op.drop_constraint("ck_dish_reviews_value_prop_range", "dish_reviews", type_="check")
    op.drop_constraint("ck_dish_reviews_presentation_range", "dish_reviews", type_="check")
    op.drop_column("dish_reviews", "execution")
    op.drop_column("dish_reviews", "value_prop")
    op.drop_column("dish_reviews", "presentation")
