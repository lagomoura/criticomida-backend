"""want_to_try_dishes table — wishlist plato↔usuario

Revision ID: 022
Revises: 021
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "want_to_try_dishes",
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dish_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("dishes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "dish_id", name="pk_want_to_try_dishes"),
    )
    op.create_index(
        "ix_want_to_try_user_created",
        "want_to_try_dishes",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_want_to_try_dish",
        "want_to_try_dishes",
        ["dish_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_want_to_try_dish", table_name="want_to_try_dishes")
    op.drop_index("ix_want_to_try_user_created", table_name="want_to_try_dishes")
    op.drop_table("want_to_try_dishes")
