"""Add 'mention' to ck_notifications_kind.

Habilita el nuevo kind ``mention`` que se inserta cuando un usuario aparece
arrobado (``@handle``) en un comentario, reply, post o respuesta del owner.

Revision ID: 044
Revises: 043
Create Date: 2026-05-06
"""

from typing import Sequence, Union

from alembic import op


revision: str = "044"
down_revision: Union[str, None] = "043"
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
    "review_on_owned_restaurant",
)
_KINDS_NEW = _KINDS_OLD + ("mention",)


def _ck_kind_clause(kinds: tuple[str, ...]) -> str:
    return "kind IN (" + ",".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
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
