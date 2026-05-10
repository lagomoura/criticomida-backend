"""Safety primitives: ``user_blocks`` and ``user_mutes``.

Backs the CRÍTICO #1 finding from the social-layer audit (a62a03a,
2026-05-10): hasta hoy un usuario no podía bloquear ni silenciar a
otro, dejando feed/notificaciones/follows sin defensa contra
harassment. Estas dos tablas son las primitivas mínimas; los puntos
de filtrado (``feed.py``, ``notification_service.py``, ``follows.py``)
las consultan a través de ``app.services.safety_service``.

Diseño:

- **user_blocks** — bidireccional en impacto. La PK
  ``(blocker_id, blocked_id)`` cubre el lookup directo "¿A bloqueó a
  B?". El índice extra sobre ``blocked_id`` cubre "¿alguien bloqueó a
  X?", que el notification guard ejecuta antes de cada insert.
- **user_mutes** — silencioso y unidireccional. La PK
  ``(muter_id, muted_id)`` cubre el caso real ("¿muter está silenciando
  a muted?"); el muted nunca consulta su propio mute, así que NO
  agregamos índice sobre ``muted_id``.
- ``CheckConstraint`` impide self-block / self-mute (mismo patrón que
  ``ck_follows_no_self`` en migración inicial).
- ``ON DELETE CASCADE`` en ambas FKs: si una de las dos partes borra su
  cuenta, la fila desaparece — no necesitamos historial de safety
  primitives.

No se crean ENUMs nuevos: ambas tablas son puro JOIN-edge.

Revision ID: 055
Revises: 054
Create Date: 2026-05-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "055"
down_revision: Union[str, None] = "054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_blocks",
        sa.Column(
            "blocker_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "blocked_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "blocker_id", "blocked_id", name="pk_user_blocks"
        ),
        sa.CheckConstraint(
            "blocker_id <> blocked_id", name="ck_user_blocks_no_self"
        ),
    )

    # "¿X fue bloqueado por alguien?" — usado por el notification guard
    # antes de cada insert: ``EXISTS (... WHERE blocked_id = actor_id
    # AND blocker_id = recipient_id)`` aprovecha la PK; pero cuando el
    # recipient varía y el actor está fijo (mention masiva, fan-out de
    # likes a una review popular), el lookup eficiente es por blocked_id.
    op.create_index(
        "ix_user_blocks_blocked_id",
        "user_blocks",
        ["blocked_id"],
    )

    op.create_table(
        "user_mutes",
        sa.Column(
            "muter_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "muted_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "muter_id", "muted_id", name="pk_user_mutes"
        ),
        sa.CheckConstraint(
            "muter_id <> muted_id", name="ck_user_mutes_no_self"
        ),
    )


def downgrade() -> None:
    op.drop_table("user_mutes")
    op.drop_index("ix_user_blocks_blocked_id", table_name="user_blocks")
    op.drop_table("user_blocks")
