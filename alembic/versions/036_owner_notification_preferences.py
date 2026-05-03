"""Owner notification preferences + new notification kind.

Crea la tabla ``owner_notification_preferences`` (PK compuesta por
``user_id`` + ``restaurant_id``) que controla si el dueño verificado del
restaurante recibe aviso (in-app + email) cuando llega una reseña nueva.
Default ON: si no hay fila, se asume ``notify_on_review=true``; el primer
toggle hace upsert.

Además extiende el CHECK de ``notifications.kind`` para sumar
``review_on_owned_restaurant`` — el nuevo kind insertado por el hook de
``POST /api/dishes/{id}/reviews``.

Revision ID: 036
Revises: 035
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "036"
down_revision: Union[str, None] = "035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_KINDS_OLD = (
    "like",
    "comment",
    "follow",
    "claim_approved",
    "claim_rejected",
    "claim_revoked",
    "comment_like",
    "comment_reply",
    "reservation_requested",
)
_KINDS_NEW = _KINDS_OLD + ("review_on_owned_restaurant",)


def _ck_kind_clause(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
    op.create_table(
        "owner_notification_preferences",
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "restaurant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "notify_on_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "restaurant_id", name="pk_owner_notification_preferences"
        ),
    )

    op.drop_constraint(
        "ck_notifications_kind", "notifications", type_="check"
    )
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        _ck_kind_clause(_KINDS_NEW),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_notifications_kind", "notifications", type_="check"
    )
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        _ck_kind_clause(_KINDS_OLD),
    )

    op.drop_table("owner_notification_preferences")
