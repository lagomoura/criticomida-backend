"""restaurant_slug_redirects table — keep old slugs reachable after admin merges

Revision ID: 021
Revises: 020
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restaurant_slug_redirects",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("old_slug", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "restaurant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_restaurant_slug_redirects_restaurant_id",
        "restaurant_slug_redirects",
        ["restaurant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_restaurant_slug_redirects_restaurant_id",
        table_name="restaurant_slug_redirects",
    )
    op.drop_table("restaurant_slug_redirects")
