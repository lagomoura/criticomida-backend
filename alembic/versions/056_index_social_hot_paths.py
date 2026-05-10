"""Hot-path indexes para la capa social (audit a62a03a, MEDIO #1/1b/1c).

Cuatro índices que el audit DB del 2026-05-10 marcó como faltantes
y de costo cero:

- ``ix_follows_following_id (following_id, created_at DESC)``
  Cubre dos hot paths: (1) listar seguidores ordenados por fecha
  (perfil público "N seguidores"), (2) el segundo salto de
  people-you-may-know (``f2.follower_id = f1.following_id``). Hoy
  ese lookup hace seq scan; con este índice queda en O(log N).

- ``ix_notifications_recipient_created (recipient_user_id, created_at DESC)``
  El inbox (``GET /api/notifications``) filtra por recipient y
  ordena por fecha. Postgres no genera índice automático del lado
  hijo de una FK; sin éste, una notif vieja del recipient cuesta un
  seq scan completo de ``notifications``.

- ``ix_notifications_unread (recipient_user_id) WHERE read_at IS NULL``
  Partial para el badge de unread (``GET /api/notifications/unread-count``).
  El subset de filas con ``read_at IS NULL`` es chico — basta un
  índice angosto para que el ``COUNT(*)`` use index-only scan.

- ``ix_bookmarks_user_created (user_id, created_at DESC)``
  La PK ``(user_id, review_id)`` no incluye ``created_at`` y no sirve
  para el listado "Mis guardados" ordenado por fecha de bookmark
  (``GET /api/users/me/bookmarks``). Sin este índice, la query
  paginada degrada a seq scan + sort.

Todos sin ``CREATE INDEX CONCURRENTLY`` porque las tablas son chicas
hoy y la migración corre en el entrypoint antes de uvicorn — el
arranque del backend tolera unos segundos extra. Si en algún momento
estos índices se tienen que recrear sobre tablas grandes, la receta
``concurrently`` se hace en una migración aparte fuera del path normal.

Revision ID: 056
Revises: 055
Create Date: 2026-05-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "056"
down_revision: Union[str, None] = "055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_follows_following_id",
        "follows",
        ["following_id", sa.text("created_at DESC")],
    )

    op.create_index(
        "ix_notifications_recipient_created",
        "notifications",
        ["recipient_user_id", sa.text("created_at DESC")],
    )

    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["recipient_user_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )

    op.create_index(
        "ix_bookmarks_user_created",
        "bookmarks",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_bookmarks_user_created", table_name="bookmarks")
    op.drop_index("ix_notifications_unread", table_name="notifications")
    op.drop_index(
        "ix_notifications_recipient_created", table_name="notifications"
    )
    op.drop_index("ix_follows_following_id", table_name="follows")
