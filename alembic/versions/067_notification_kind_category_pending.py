"""notifications: extender CHECK constraint con `category_pending_review`

Habilita el dispatch a admins cuando el servicio de inferencia auto-crea
una categoría nueva. Postgres no permite extender un CHECK existente,
así que dropeamos y recreamos con el set ampliado.

Revision ID: 067
Revises: 066
Create Date: 2026-05-13
"""

from typing import Union

from alembic import op

revision: str = "067"
down_revision: Union[str, None] = "066"
branch_labels = None
depends_on = None


_OLD_KINDS = (
    "'like','comment','follow','claim_approved',"
    "'claim_rejected','claim_revoked','comment_like','comment_reply',"
    "'reservation_requested','review_on_owned_restaurant','mention',"
    "'sommelier_review_recall'"
)

_NEW_KINDS = _OLD_KINDS + ",'category_pending_review'"


def upgrade() -> None:
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        f"kind IN ({_NEW_KINDS})",
    )


def downgrade() -> None:
    # Si hubiera filas con kind='category_pending_review' las "rebajamos"
    # a 'mention' antes de re-cerrar el CHECK al set viejo. Mantenemos
    # el row para no perder la traza; el texto del notification queda.
    op.execute(
        "UPDATE notifications SET kind = 'mention' "
        "WHERE kind = 'category_pending_review'"
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        f"kind IN ({_OLD_KINDS})",
    )
