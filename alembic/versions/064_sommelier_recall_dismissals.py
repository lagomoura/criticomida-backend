"""064 — Sommelier recall dismissals (Post-visit Bridge escape hatch).

Tabla para que el comensal pueda "X" una recomendación pendiente del
Sommelier sin necesidad de reseñar el plato. Cuando dismissa, el dish
no vuelve a aparecer en la sección "Pendientes de reseñar" del empty
state ni siquiera si el Sommelier lo re-recomienda en una conversación
futura — el dismiss es permanente por diseño (DMMT: "una X significa
no quiero ver más esto", consistente con la metáfora universal).

Tabla aparte en lugar de un array en ``user_ui_state``:

- Patrón consistente con ``user_blocks``, ``user_mutes``,
  ``bookmarks``: PK compuesta ``(user_id, dish_id)``, FKs con
  ``ON DELETE CASCADE``.
- Permite agregar metadata (``dismissed_at``) sin inflar la row del
  user.
- Habilita métricas naturales ("¿qué % de recalls termina en
  dismiss?") sin hacer ``array_length`` sobre un blob.
- Crecimiento acotado: a lo sumo un row por (user, dish) — el
  ``ON CONFLICT DO NOTHING`` del endpoint impide duplicados.

Revision ID: 064
Revises: 063
Create Date: 2026-05-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "064"
down_revision: Union[str, None] = "063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sommelier_recall_dismissals",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dish_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dishes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "dish_id", name="pk_sommelier_recall_dismissals"
        ),
    )
    # The PK serves the (user_id, dish_id) lookup the recall filter
    # does (NOT EXISTS); no secondary index needed at this scale.


def downgrade() -> None:
    op.drop_table("sommelier_recall_dismissals")
