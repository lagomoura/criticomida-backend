"""restaurant_claims — flujo de reclamación de ficha por dueño

Pieza estructural del pilar B2B. Permite que un dueño verifique su autoría
sobre la ficha del restaurant. Las features que dependen del flag de verified
owner (responder reviews, fotos oficiales, sponsored, analytics) cuelgan de
acá.

Revision ID: 024
Revises: 023
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restaurant_claims",
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
        sa.Column(
            "claimant_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("verification_method", sa.String(24), nullable=False),
        sa.Column(
            "verification_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "evidence_urls",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
        ),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by_admin_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_restaurant_claims_restaurant",
        "restaurant_claims",
        ["restaurant_id"],
    )
    op.create_index(
        "ix_restaurant_claims_claimant",
        "restaurant_claims",
        ["claimant_user_id"],
    )
    # Solo un dueño verificado activo por restaurant
    op.create_index(
        "uq_restaurant_claims_verified",
        "restaurant_claims",
        ["restaurant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'verified'"),
    )
    # Un user no puede tener dos claims abiertos del mismo local en simultáneo
    op.create_index(
        "uq_restaurant_claims_open_per_user",
        "restaurant_claims",
        ["restaurant_id", "claimant_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'verifying')"),
    )

    op.add_column(
        "restaurants",
        sa.Column(
            "claimed_by_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "restaurants",
        sa.Column(
            "claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_restaurants_claimed_by_user",
        "restaurants",
        ["claimed_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_restaurants_claimed_by_user", table_name="restaurants")
    op.drop_column("restaurants", "claimed_at")
    op.drop_column("restaurants", "claimed_by_user_id")
    op.drop_index(
        "uq_restaurant_claims_open_per_user", table_name="restaurant_claims"
    )
    op.drop_index(
        "uq_restaurant_claims_verified", table_name="restaurant_claims"
    )
    op.drop_index(
        "ix_restaurant_claims_claimant", table_name="restaurant_claims"
    )
    op.drop_index(
        "ix_restaurant_claims_restaurant", table_name="restaurant_claims"
    )
    op.drop_table("restaurant_claims")
