"""Extiende notification kinds con claim_approved/rejected/revoked + target_restaurant_id

Permite enchufar in-app notifications al stub notify_claimant del Hito 4.
El claimant recibe una fila en la campanita cuando un admin aprueba,
rechaza o revoca su reclamo.

Revision ID: 027
Revises: 026
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


_NEW_CONSTRAINT = (
    "kind IN ('like','comment','follow','claim_approved',"
    "'claim_rejected','claim_revoked')"
)
_OLD_CONSTRAINT = "kind IN ('like','comment','follow')"


def upgrade() -> None:
    op.drop_constraint(
        "ck_notifications_kind", "notifications", type_="check"
    )
    op.create_check_constraint(
        "ck_notifications_kind", "notifications", _NEW_CONSTRAINT
    )

    op.add_column(
        "notifications",
        sa.Column(
            "target_restaurant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_notifications_target_restaurant",
        "notifications",
        ["target_restaurant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notifications_target_restaurant", table_name="notifications"
    )
    op.drop_column("notifications", "target_restaurant_id")

    op.drop_constraint(
        "ck_notifications_kind", "notifications", type_="check"
    )
    op.create_check_constraint(
        "ck_notifications_kind", "notifications", _OLD_CONSTRAINT
    )
