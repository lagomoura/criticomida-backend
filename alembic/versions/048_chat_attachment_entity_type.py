"""entity_type: add chat_attachment value

Revision ID: 048
Revises: 047
Create Date: 2026-05-07
"""

from typing import Union

from alembic import op


revision: str = "048"
down_revision: Union[str, None] = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``ALTER TYPE ... ADD VALUE`` no puede correr dentro de una
    # transacción en Postgres < 12; con 12+ funciona pero queda
    # auto-committed igual. ``IF NOT EXISTS`` lo hace idempotente
    # frente a re-runs del backfill (CI re-aplica migraciones).
    op.execute(
        "ALTER TYPE entity_type ADD VALUE IF NOT EXISTS 'chat_attachment'"
    )


def downgrade() -> None:
    # Postgres no soporta DROP VALUE en un enum sin recrear el tipo
    # entero. Las filas históricas con ``chat_attachment`` quedarían
    # huérfanas y la operación rara vez justifica el costo, así que
    # el downgrade es no-op intencional.
    pass
