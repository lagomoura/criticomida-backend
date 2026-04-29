"""enable pg_trgm + unaccent extensions for fuzzy name matching of restaurants

Revision ID: 019
Revises: 018
Create Date: 2026-04-29
"""

from alembic import op


revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pg_trgm: trigram similarity (`similarity()`).
    # unaccent: strips diacritics so "Güerrín" and "Guerrin" compare as equal.
    # Both are required by `find_match_candidates` (Fase 2.2 dedup).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")


def downgrade() -> None:
    # Don't drop extensions on downgrade — other tables/indexes may rely on
    # them once present, and DROP EXTENSION ... CASCADE is too blunt.
    pass
