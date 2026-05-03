"""Widen notifications.kind from VARCHAR(20) to VARCHAR(40).

El kind ``review_on_owned_restaurant`` mide 26 caracteres y no entra en el
límite original de 20. La 036 sumó el valor al CHECK constraint pero se
omitió la ampliación del tipo. Esta migración corrige ese gap. La columna
sigue manteniendo el CHECK que limita los valores válidos, así que no hay
riesgo de aceptar kinds basura.

Revision ID: 037
Revises: 036
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "037"
down_revision: Union[str, None] = "036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "notifications",
        "kind",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "notifications",
        "kind",
        existing_type=sa.String(length=40),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
