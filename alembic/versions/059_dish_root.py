"""dish root extraction: function + generated column + indexes

Revision ID: 059
Revises: 058
Create Date: 2026-05-11

Why:
    El duelo de platos enfrenta "el mismo plato base" en restaurantes distintos
    (ej. "Sorrentinos de jamón crudo" vs "Sorrentinos al pomodoro" en otro
    local). Necesitamos una clave estable para agrupar dishes por su raíz
    semántica, sin depender de la categoría del restaurante (que mezclaba
    pizzas con postres en pizzerías).

What:
    1. Función IMMUTABLE `public.dish_root_extract(text)` que toma un texto
       ya normalizado (lower + unaccent + collapsed spaces) y devuelve el
       primer token útil:
       - elimina paréntesis y su contenido
       - tokeniza por espacios
       - descarta stopwords ES/IT/EN y tokens-basura ("plato", "combo", ...)
       - devuelve el primer token restante o NULL si quedó vacío
       - trunca a 64 chars
    2. Columna `dishes.dish_root` como STORED generated derivada de
       `name` (no de `name_normalized`: Postgres prohíbe que una generated
       column referencie a otra generated column). La derivación encadena
       las dos funciones inline:
         dish_root_extract(dish_name_normalized(name))
       Al editar `name`, la columna se recalcula sola — sin backfill,
       sin trigger.
    3. Índice parcial b-tree para lookups exactos por root.
    4. Índice gin trgm para el endpoint de roots populares (fuzzy).

Refinamiento futuro:
    Si la heurística cambia, hay que `ALTER COLUMN dish_root DROP EXPRESSION`
    + re-ADD con la expresión nueva en una migración posterior. `pg_dump`
    preserva el valor materializado, así que dev/prod no se re-sincronizan
    sin la migración de re-derivación.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "059"
down_revision: Union[str, None] = "058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Función IMMUTABLE para extraer la raíz semántica del nombre.
    # Usa el `name_normalized` que ya existe (migración 020), por lo que la
    # entrada está garantizada en lower + sin acentos + espacios colapsados.
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.dish_root_extract(text)
            RETURNS text
            LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
            AS $$
                WITH
                no_parens AS (
                    SELECT regexp_replace($1, '\([^)]*\)', '', 'g') AS s
                ),
                tokens AS (
                    SELECT regexp_split_to_table(trim(s), '\s+') AS tok
                    FROM no_parens
                ),
                useful AS (
                    SELECT tok
                    FROM tokens
                    WHERE tok <> ''
                      AND tok NOT IN (
                          -- stopwords ES
                          'de','del','la','el','los','las','un','una','unos','unas',
                          'con','sin','al','a','en','y','o','para','por','su','sus',
                          -- stopwords IT
                          'di','alla','allo','agli','alle','col','dei','delle','degli',
                          -- stopwords EN
                          'the','of','with','and','to','for','in','on',
                          -- tokens-basura (no aportan identidad de plato)
                          'plato','platos','combo','combos','especial','especiales',
                          'menu','menú','dia','día','casa','chef','clasico','clasica',
                          'tradicional','nuevo','nueva','mini'
                      )
                )
                SELECT left(tok, 64) FROM useful LIMIT 1
            $$
        """
    )

    # 2. Generated STORED column. Postgres no permite encadenar generated
    # columns (`name_normalized` ya es generated), así que llamamos las dos
    # funciones inline sobre `name`. El resultado es idéntico a lo que
    # haría una cadena `name → name_normalized → dish_root` porque ambas
    # funciones son IMMUTABLE.
    op.execute(
        """
        ALTER TABLE dishes
            ADD COLUMN dish_root text
            GENERATED ALWAYS AS (
                public.dish_root_extract(public.dish_name_normalized(name))
            ) STORED
        """
    )

    # 3. Índice parcial b-tree para lookups exactos.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dishes_dish_root
            ON dishes (dish_root)
            WHERE dish_root IS NOT NULL
        """
    )

    # 4. Índice gin trgm para roots populares + fuzzy match si hace falta.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dishes_dish_root_trgm
            ON dishes USING gin (dish_root gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dishes_dish_root_trgm")
    op.execute("DROP INDEX IF EXISTS ix_dishes_dish_root")
    op.execute("ALTER TABLE dishes DROP COLUMN IF EXISTS dish_root")
    op.execute("DROP FUNCTION IF EXISTS public.dish_root_extract(text)")
