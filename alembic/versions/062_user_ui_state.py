"""Tabla user_ui_state — estado de UI persistente por usuario (B2C).

Aislada de ``user_chat_preferences`` para no contaminar el system
prompt del Sommelier con datos puramente de UI (e.g. tours descartados).
Una fila por user; ``dismissed_tours`` es un array de identificadores
de tours que el usuario ya cerró.

Revision ID: 062
Revises: 061
Create Date: 2026-05-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "062"
down_revision: Union[str, None] = "061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_ui_state",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # text[] (no JSONB) — más idiomático para arrays de strings,
        # array_append / array_remove / array_agg(DISTINCT ...) atómicos.
        sa.Column(
            "dismissed_tours",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
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
        sa.PrimaryKeyConstraint("user_id", name="pk_user_ui_state"),
    )


def downgrade() -> None:
    op.drop_table("user_ui_state")
