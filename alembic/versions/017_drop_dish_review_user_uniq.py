"""drop unique (dish_id, user_id) on dish_reviews so a user can review the same dish multiple times

Revision ID: 017
Revises: 016
Create Date: 2026-04-29
"""

from alembic import op


revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_dish_user_review", "dish_reviews", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_dish_user_review",
        "dish_reviews",
        ["dish_id", "user_id"],
    )
