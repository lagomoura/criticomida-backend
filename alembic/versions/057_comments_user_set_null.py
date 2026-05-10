"""``comments.user_id`` ON DELETE CASCADE → SET NULL.

Cierra el hallazgo BAJO #1 del audit social (a62a03a). Política
decidida (2026-05-10): cuando un user solicita borrado GDPR, sus
**comentarios sobreviven anónimos** — el body queda intacto, el
``user_id`` pasa a NULL. Mismo criterio que ``dish_reviews.user_id``
(migración 051), preservando el contexto del hilo (replies sin
parent rompen la conversación).

Diferencia con migración 051: aquella tocaba 7 FKs de autoría
informativa de una vez. Acá solo toca ``comments.user_id`` porque era
la única FK que quedó con CASCADE tras 051.

Detalles:
- ``comments.user_id`` pasa a ``nullable=True`` para aceptar NULL
  post-cascade.
- FK recreada con ``ON DELETE SET NULL``. Nombre Postgres-default:
  ``comments_user_id_fkey``.
- El downgrade revierte solo el ondelete, NO el nullable — si después
  del deploy hay rows con NULL (users borrados), un downgrade que
  rechace NULLs bloquearía el rollback.

Revision ID: 057
Revises: 056
Create Date: 2026-05-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "057"
down_revision: Union[str, None] = "056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("comments_user_id_fkey", "comments", type_="foreignkey")
    op.alter_column(
        "comments", "user_id", existing_type=UUID(as_uuid=True), nullable=True
    )
    op.create_foreign_key(
        "comments_user_id_fkey",
        "comments",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("comments_user_id_fkey", "comments", type_="foreignkey")
    op.create_foreign_key(
        "comments_user_id_fkey",
        "comments",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Intencional: no revertimos ``nullable=True`` → ``nullable=False`` —
    # ver docstring.
