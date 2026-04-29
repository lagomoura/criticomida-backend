"""dish name normalization: extensions, generated column, unique + trigram indexes

Revision ID: 020
Revises: 019
Create Date: 2026-04-29

Why:
    Dedup dishes typed with different casing/spacing/accents within the same
    restaurant. "Muzzarella" / "muzzarella " / "MUZZARELLA" / "Muzzarela"
    used to create separate Dish rows; now they collide on a normalized form.

What:
    1. Enable `unaccent` + `pg_trgm` extensions.
    2. Add IMMUTABLE wrappers so we can use them in generated columns / indexes.
    3. Add `dishes.name_normalized` as a STORED generated column (always derived
       from `name` — never set by application code).
    4. Pre-check existing data for collisions and raise with a clear list before
       the unique index would silently fail.
    5. Create unique index `(restaurant_id, name_normalized)` and a trigram GIN
       index on `name_normalized` for fuzzy "did-you-mean" queries.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Extensions ----------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2. IMMUTABLE wrappers --------------------------------------------------
    # `unaccent()` is STABLE because it depends on a dictionary; we pin the
    # dictionary explicitly so the wrapper is safe to use in expression indexes
    # and generated columns. Pattern from the Postgres docs / Erwin Brandstetter.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.f_unaccent(text)
            RETURNS text
            LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
            AS $$ SELECT public.unaccent('public.unaccent', $1) $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.dish_name_normalized(text)
            RETURNS text
            LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
            AS $$
                SELECT lower(public.f_unaccent(regexp_replace(trim($1), '\\s+', ' ', 'g')))
            $$
        """
    )

    # 3. Pre-check for duplicates that would break the unique index ----------
    op.execute(
        """
        DO $$
        DECLARE
            dup_summary text;
        BEGIN
            SELECT string_agg(
                format(
                    'restaurant_id=%s name_normalized=%L count=%s',
                    restaurant_id, normalized, cnt
                ),
                E'\\n'
            )
            INTO dup_summary
            FROM (
                SELECT
                    restaurant_id,
                    public.dish_name_normalized(name) AS normalized,
                    count(*) AS cnt
                FROM dishes
                GROUP BY restaurant_id, public.dish_name_normalized(name)
                HAVING count(*) > 1
            ) AS dups;

            IF dup_summary IS NOT NULL THEN
                RAISE EXCEPTION
                    'Existing duplicate dishes (same restaurant, same normalized name) block this migration. Merge them first via admin tools, then re-run.\\n%',
                    dup_summary;
            END IF;
        END $$
        """
    )

    # 4. Generated column ----------------------------------------------------
    op.execute(
        """
        ALTER TABLE dishes
            ADD COLUMN name_normalized text
            GENERATED ALWAYS AS (public.dish_name_normalized(name)) STORED
        """
    )

    # 5. Indexes -------------------------------------------------------------
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dishes_restaurant_name_normalized
            ON dishes (restaurant_id, name_normalized)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_dishes_name_normalized_trgm
            ON dishes USING gin (name_normalized gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dishes_name_normalized_trgm")
    op.execute("DROP INDEX IF EXISTS uq_dishes_restaurant_name_normalized")
    op.execute("ALTER TABLE dishes DROP COLUMN IF EXISTS name_normalized")
    op.execute("DROP FUNCTION IF EXISTS public.dish_name_normalized(text)")
    op.execute("DROP FUNCTION IF EXISTS public.f_unaccent(text)")
    # Extensions left in place — other code may rely on them.
