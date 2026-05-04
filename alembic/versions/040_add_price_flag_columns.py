"""Capa 2 anti-fraude: columnas de flag de precio en dish_reviews.

Cuando un precio cargado por el crítico se desvía mucho del histórico del
plato (outlier por mediana × ratio), no rechazamos la reseña — el texto y el
rating pueden seguir siendo valiosos. En cambio, soft-flageamos el precio:
``price_flagged_at`` queda con timestamp y ``price_flag_reason`` con el motivo.

El timeline (`get_dish_timeline`) excluye las reseñas con `price_flagged_at`
no nulo del cálculo del avg de precio, así un outlier no contamina la métrica
hasta que un humano lo confirme o invalide.

`price_flag_resolved_at` + `price_flag_resolved_by` permiten que un admin u
owner revierta el flag (la review queda visible y vuelve al avg) o lo
confirme (la review se mantiene, pero el precio queda permanentemente
excluido). Esto es la base para la fase (c): notificación + UI de revisión.

Revision ID: 040
Revises: 039
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "040"
down_revision: Union[str, None] = "039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dish_reviews",
        sa.Column("price_flagged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dish_reviews",
        sa.Column("price_flag_reason", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "dish_reviews",
        sa.Column(
            "price_flag_resolved_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "dish_reviews",
        sa.Column(
            "price_flag_resolved_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("dish_reviews", "price_flag_resolved_by")
    op.drop_column("dish_reviews", "price_flag_resolved_at")
    op.drop_column("dish_reviews", "price_flag_reason")
    op.drop_column("dish_reviews", "price_flagged_at")
