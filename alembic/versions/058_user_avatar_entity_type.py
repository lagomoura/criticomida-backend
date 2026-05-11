"""Extiende el PG enum ``entity_type`` con ``user_avatar``.

Habilita el flujo de subida de foto de perfil: el endpoint
``POST /api/images/upload`` exige que ``entity_type`` pertenezca al
enum, y hasta ahora solo cubría restaurant/dish/chat. El router suma
en paralelo un guard que obliga a ``entity_id == current_user.id``
cuando el tipo es ``user_avatar`` (un usuario solo sube avatar para
sí mismo).

Notas de Postgres:
- ``ALTER TYPE … ADD VALUE`` requiere PG ≥9.1; desde PG 12 también
  funciona dentro de una transacción ordinaria. Railway corre PG ≥13,
  así que no hace falta abrir un autocommit_block.
- ``IF NOT EXISTS`` hace la migración idempotente y deja correr el
  upgrade aunque alguien haya agregado el valor a mano antes.

Revision ID: 058
Revises: 057
Create Date: 2026-05-10
"""

from typing import Sequence, Union

from alembic import op


revision: str = "058"
down_revision: Union[str, None] = "057"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE entity_type ADD VALUE IF NOT EXISTS 'user_avatar'")


def downgrade() -> None:
    # Postgres no soporta eliminar un valor de un enum sin recrear el
    # tipo y reescribir todas las columnas que lo usan. El downgrade es
    # no-op intencional: revertir el código que usa ``user_avatar`` es
    # suficiente, y el valor extra en el enum no rompe nada.
    pass
