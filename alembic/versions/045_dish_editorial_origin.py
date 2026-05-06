"""Add editorial origin chip + prompt version to dishes.

`editorial_origin` persiste la etiqueta de cocina/tradición que el LLM devuelve
junto al blurb (ej.: "Cocina napolitana"). Se renderiza como chip arriba del
quote en `EditorialStoryCard`.

`editorial_prompt_version` permite invalidar blurbs viejos cuando cambia el
prompt o el formato de salida — el script de backfill regenera todo lo que no
matchee la versión actual.

Revision ID: 045
Revises: 044
Create Date: 2026-05-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "045"
down_revision: Union[str, None] = "044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dishes",
        sa.Column("editorial_origin", sa.String(80), nullable=True),
    )
    op.add_column(
        "dishes",
        sa.Column("editorial_prompt_version", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dishes", "editorial_prompt_version")
    op.drop_column("dishes", "editorial_origin")
