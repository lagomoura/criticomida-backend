"""Tabla owner_chat_preferences para personalización del chat Business.

Una fila por par ``(user_id, restaurant_id)``. Sin fila → defaults
(tono profesional, idioma del input, sin KPIs prioritarios). Se crea
on-demand cuando el owner pide algo persistente al chatbot vía el
tool ``update_owner_preferences``.

Tabla separada de ``owner_notification_preferences`` (otro feature, otro
ciclo de update). Mismo (user_id, restaurant_id) compuesta como PK.

Revision ID: 041
Revises: 040
Create Date: 2026-05-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "041"
down_revision: Union[str, None] = "040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "owner_chat_preferences",
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "restaurant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tone_preference", sa.String(length=32), nullable=True),
        sa.Column(
            "kpi_focus", sa.dialects.postgresql.JSONB(), nullable=True
        ),
        sa.Column("language_preference", sa.String(length=8), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "user_id", "restaurant_id", name="pk_owner_chat_preferences"
        ),
    )


def downgrade() -> None:
    op.drop_table("owner_chat_preferences")
