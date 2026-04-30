"""Agrega 'restaurant_official_photo' al enum entity_type

Resuelve la duplicación que tenía el flujo del Hito 6: el owner subía fotos
oficiales pasando por /api/images/upload con entity_type='restaurant_gallery',
lo que las dejaba además visibles en la galería pública del local.

Con este nuevo valor del enum, el upload del owner queda aislado de la
gallery pública y solo se muestra como official_photo en el hero.

Revision ID: 026
Revises: 025
Create Date: 2026-04-30
"""

from alembic import op


revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE no corre dentro de una transacción en
    # Postgres < 12. La autocommit_block lo asegura cross-versión.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE entity_type ADD VALUE IF NOT EXISTS 'restaurant_official_photo'"
        )


def downgrade() -> None:
    # Postgres no soporta DROP VALUE de un enum sin recrear el tipo. Como
    # este valor es aditivo y no rompe nada, dejamos el downgrade como no-op.
    pass
