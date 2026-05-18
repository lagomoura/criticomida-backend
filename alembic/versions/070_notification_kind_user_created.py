"""notifications: extender CHECK constraint con `user_created`

Habilita el dispatch a admins cuando se registra un usuario nuevo.
Postgres no permite extender un CHECK existente, así que dropeamos y
recreamos con el set ampliado (mismo patrón que la migración 067).

Revision ID: 070
Revises: 069
Create Date: 2026-05-17
"""

from typing import Union

from alembic import op

revision: str = "070"
down_revision: Union[str, None] = "069"
branch_labels = None
depends_on = None


_OLD_KINDS = (
    "'like','comment','follow','claim_approved',"
    "'claim_rejected','claim_revoked','comment_like','comment_reply',"
    "'reservation_requested','review_on_owned_restaurant','mention',"
    "'sommelier_review_recall','category_pending_review'"
)

_NEW_KINDS = _OLD_KINDS + ",'user_created'"


def upgrade() -> None:
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        f"kind IN ({_NEW_KINDS})",
    )


def downgrade() -> None:
    # Si hubiera filas con kind='user_created' las "rebajamos" a 'mention'
    # antes de re-cerrar el CHECK al set viejo. Mantenemos el row para no
    # perder la traza; el texto del notification queda.
    op.execute(
        "UPDATE notifications SET kind = 'mention' "
        "WHERE kind = 'user_created'"
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        f"kind IN ({_OLD_KINDS})",
    )
