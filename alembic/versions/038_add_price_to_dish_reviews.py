"""Agrega price_paid (Numeric 12,2) opcional a dish_reviews.

Captura cuánto pagó el crítico por el plato en cada reseña. Es opcional
porque no siempre se conoce el precio (cortesía, menú degustación, reseñas
históricas). El timeline de evolución del plato lo agrega como avg por
bucket — reseñas con price_paid IS NULL se ignoran del avg, igual que los
pilares NULL.

Numeric(12, 2) cubre precios grandes en monedas devaluadas (ARS llega a
millones por plato).

Revision ID: 038
Revises: 037
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "038"
down_revision: Union[str, None] = "037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dish_reviews",
        sa.Column("price_paid", sa.Numeric(12, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_dish_reviews_price_paid_positive",
        "dish_reviews",
        "price_paid IS NULL OR price_paid > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_dish_reviews_price_paid_positive", "dish_reviews", type_="check"
    )
    op.drop_column("dish_reviews", "price_paid")
