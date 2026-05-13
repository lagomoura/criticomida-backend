"""dishes: agregar category_id propio (FK nullable a categories)

Modelo conceptual: el restaurant tiene UNA categoría (su identidad: 'parrilla',
'china', 'japonesa'), y cada plato puede tener la suya — un restaurant chino
puede servir un Khachapuri georgiano, una parrilla puede tener gnocchi
italianos. Antes el dish heredaba implícitamente la categoría del restaurant
vía el JOIN del feed; con este cambio el dish gana identidad propia y la
inferencia auto persiste a nivel dish (no toca la categoría del restaurant).

Sin backfill automático: los dishes existentes quedan con category_id NULL y
el feed cae al category_id del restaurant. La capa de presentación coalesce
ambos (dish > restaurant), así nada se rompe para datos previos.

Revision ID: 068
Revises: 067
Create Date: 2026-05-13
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "068"
down_revision: Union[str, None] = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dishes",
        sa.Column("category_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_dishes_category_id",
        "dishes",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Index parcial: la mayoría de queries que filtran por categoría a nivel
    # dish van a tener WHERE category_id = X. Skip filas NULL para mantener
    # el índice chico mientras el rollout backfillea gradualmente.
    op.create_index(
        "ix_dishes_category_id",
        "dishes",
        ["category_id"],
        postgresql_where=sa.text("category_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dishes_category_id",
        table_name="dishes",
        postgresql_where=sa.text("category_id IS NOT NULL"),
    )
    op.drop_constraint("fk_dishes_category_id", "dishes", type_="foreignkey")
    op.drop_column("dishes", "category_id")
