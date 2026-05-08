"""User FKs: ON DELETE SET NULL en autoría informativa.

Audit-driven: MEDIO #2/#3 del audit DB de 2026-05-08.

Hoy 7 FKs apuntan a ``users.id`` con default ``NO ACTION``, lo que
bloquea cualquier hard-delete de usuario (GDPR, limpieza de cuentas
abandonadas) con ``ForeignKeyViolation``. La política decidida es
**SET NULL para autoría informativa**: las reseñas y contenido se
preservan como anónimos cuando se borra al user.

Tablas afectadas:
- ``dishes.created_by``
- ``dish_reviews.user_id``
- ``restaurants.created_by``
- ``restaurant_rating_dimensions.user_id``
- ``restaurant_pros_cons.user_id``
- ``visit_diary_entries.created_by``

Y como bonus, ``restaurants.category_id`` (FK a ``categories.id``,
hoy también ``NO ACTION``): cuando se deprecate una categoría no
queremos que rompa la creación de restaurantes huérfanos. Esa
columna ya es nullable desde una migración anterior.

Revision ID: 051
Revises: 050
Create Date: 2026-05-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "051"
down_revision: Union[str, None] = "050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, fk_constraint_name, fk_target_table, fk_target_column,
#  was_nullable_before)
# Los nombres de FK son los autogenerados por Postgres en
# ``001_initial_schema.py`` (anonymous FK → ``<tabla>_<columna>_fkey``).
USER_FKS = [
    ("dishes", "created_by", "dishes_created_by_fkey", "users", "id", False),
    ("dish_reviews", "user_id", "dish_reviews_user_id_fkey", "users", "id", False),
    ("restaurants", "created_by", "restaurants_created_by_fkey", "users", "id", False),
    (
        "restaurant_rating_dimensions",
        "user_id",
        "restaurant_rating_dimensions_user_id_fkey",
        "users",
        "id",
        False,
    ),
    (
        "restaurant_pros_cons",
        "user_id",
        "restaurant_pros_cons_user_id_fkey",
        "users",
        "id",
        False,
    ),
    (
        "visit_diary_entries",
        "created_by",
        "visit_diary_entries_created_by_fkey",
        "users",
        "id",
        False,
    ),
]


def upgrade() -> None:
    # 1. Drop FKs anónimas + recrear con ON DELETE SET NULL.
    for table, col, fk_name, target_table, target_col, was_nullable in USER_FKS:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        if not was_nullable:
            op.alter_column(table, col, existing_type=UUID(as_uuid=True), nullable=True)
        op.create_foreign_key(
            fk_name,
            table,
            target_table,
            [col],
            [target_col],
            ondelete="SET NULL",
        )

    # 2. ``restaurants.category_id`` ya es nullable; solo recreamos la FK
    #    con ondelete. Es Integer, no UUID.
    op.drop_constraint(
        "restaurants_category_id_fkey", "restaurants", type_="foreignkey"
    )
    op.create_foreign_key(
        "restaurants_category_id_fkey",
        "restaurants",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Revierte ondelete a NO ACTION. NO revertimos ``nullable=True`` →
    # ``nullable=False`` porque post-deploy puede haber rows con NULL
    # legítimas (usuarios borrados); un downgrade que los rechace
    # bloquearía el rollback. Si necesitás recuperar el NOT NULL hay
    # que hacer una migración data-only que limpie los NULLs primero.
    op.drop_constraint(
        "restaurants_category_id_fkey", "restaurants", type_="foreignkey"
    )
    op.create_foreign_key(
        "restaurants_category_id_fkey",
        "restaurants",
        "categories",
        ["category_id"],
        ["id"],
    )
    for table, col, fk_name, target_table, target_col, _was_nullable in reversed(
        USER_FKS
    ):
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name,
            table,
            target_table,
            [col],
            [target_col],
        )
