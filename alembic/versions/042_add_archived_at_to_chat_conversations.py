"""Soft-delete column para chat_conversations.

Una conversación archivada no aparece en ``listMyConversations`` por
default, pero conserva todo su contenido — el owner puede pedir verla
explícitamente (o un admin puede recuperarla en caso de duda). Es la
política recomendada cuando el contenido tiene valor analítico
recurrente: una respuesta del agente Business sobre un plato puntual
puede ser útil meses después.

Revision ID: 042
Revises: 041
Create Date: 2026-05-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "042"
down_revision: Union[str, None] = "041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_conversations",
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_conversations", "archived_at")
