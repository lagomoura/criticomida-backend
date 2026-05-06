"""Shared cache for dish editorial blurbs.

Muchos restaurantes sirven el mismo plato (asado, milanesa, sushi). El blurb
editorial habla del plato y su tradición — es independiente del local — así
que tiene sentido cachear por `(name_normalized, cuisine_key)` y reusar entre
restaurantes.

Ahorra tokens (una sola llamada al LLM por plato distinto, no por instancia)
y unifica el storytelling: la "milanesa napolitana" cuenta la misma historia
en todos lados de la plataforma.

Revision ID: 046
Revises: 045
Create Date: 2026-05-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "046"
down_revision: Union[str, None] = "045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dish_editorial_cache",
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("cuisine_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("story", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(80), nullable=True),
        sa.Column("prompt_version", sa.String(16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("name_key", "cuisine_key", name="pk_dish_editorial_cache"),
    )


def downgrade() -> None:
    op.drop_table("dish_editorial_cache")
