"""Tabla user_chat_preferences — Sommelier persistent preferences (B2C).

Mirror of ``owner_chat_preferences`` for the B2C agent. One row per
``user_id`` (no restaurant scope — the Sommelier sees the whole catalog).
Sin fila → defaults: el agente adapta el idioma al input y usa el
estilo "editorial" del prompt. La fila se crea on-demand cuando el
comensal pide algo persistente al chatbot vía el tool
``update_user_chat_preferences``.

Revision ID: 043
Revises: 042
Create Date: 2026-05-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "043"
down_revision: Union[str, None] = "042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_chat_preferences",
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Locale code: 'es' / 'en' / 'pt'. NULL → adapt to input.
        sa.Column("language_preference", sa.String(length=8), nullable=True),
        # Response shape: 'editorial' (default 2-3 sentence framing),
        # 'concise' (1 sentence + cards), 'warm' (more conversational).
        sa.Column("response_style", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_user_chat_preferences"),
    )


def downgrade() -> None:
    op.drop_table("user_chat_preferences")
