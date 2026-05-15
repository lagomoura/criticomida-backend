"""Per-language dish editorial blurbs.

El blurb editorial ("La historia de este plato") se generaba solo en español.
Cuando el usuario cambia el idioma de la UI (es/en/pt) el encabezado se traducía
pero el cuerpo seguía en español.

Agregamos `lang` a la cache compartida y lo metemos en la PK para que cada
idioma tenga su propia historia cacheada. Las filas existentes quedan como
`'es'` (el único idioma que se generaba), así que la cache española se preserva.

Revision ID: 069
Revises: 068
Create Date: 2026-05-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "069"
down_revision: Union[str, None] = "068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dish_editorial_cache",
        sa.Column(
            "lang",
            sa.String(8),
            nullable=False,
            server_default="es",
        ),
    )
    op.drop_constraint(
        "pk_dish_editorial_cache", "dish_editorial_cache", type_="primary"
    )
    op.create_primary_key(
        "pk_dish_editorial_cache",
        "dish_editorial_cache",
        ["name_key", "cuisine_key", "lang"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "pk_dish_editorial_cache", "dish_editorial_cache", type_="primary"
    )
    # Colapsar a una fila por (name_key, cuisine_key) antes de re-crear la PK
    # vieja: nos quedamos con la española si existe, si no la de menor ctid.
    op.execute(
        """
        DELETE FROM dish_editorial_cache
        WHERE ctid NOT IN (
            SELECT DISTINCT ON (name_key, cuisine_key) ctid
            FROM dish_editorial_cache
            ORDER BY name_key, cuisine_key, (lang = 'es') DESC, ctid
        )
        """
    )
    op.create_primary_key(
        "pk_dish_editorial_cache",
        "dish_editorial_cache",
        ["name_key", "cuisine_key"],
    )
    op.drop_column("dish_editorial_cache", "lang")
