"""dish_review_owner_responses + restaurant_official_photos

Permisos desbloqueados al verified owner (Hito 6 del roadmap B2B):
1. Una respuesta del restaurante por review (editable, una sola fila por review).
2. Fotos oficiales del local subidas por el owner — separadas de las fotos de
   comensales para mantener trazabilidad y control de quién puede borrar.

Revision ID: 025
Revises: 024
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dish_review_owner_responses",
        sa.Column(
            "review_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "owner_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_dish_review_owner_responses_owner",
        "dish_review_owner_responses",
        ["owner_user_id"],
    )

    op.create_table(
        "restaurant_official_photos",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "restaurant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("alt_text", sa.String(300), nullable=True),
        sa.Column(
            "display_order", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "uploaded_by_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_restaurant_official_photos_restaurant",
        "restaurant_official_photos",
        ["restaurant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_restaurant_official_photos_restaurant",
        table_name="restaurant_official_photos",
    )
    op.drop_table("restaurant_official_photos")
    op.drop_index(
        "ix_dish_review_owner_responses_owner",
        table_name="dish_review_owner_responses",
    )
    op.drop_table("dish_review_owner_responses")
