"""categories: add pending_review flag + partial index

Permite que el flujo de inferencia (POST /api/posts) auto-cree categorías
nuevas con `pending_review = TRUE`. El admin las cura desde la cola
`/admin/categorias-pendientes` antes de que se expongan al feed público.

Revision ID: 065
Revises: 064
Create Date: 2026-05-13
"""

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "065"
down_revision: Union[str, None] = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "categories",
        sa.Column(
            "pending_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Partial index: la cola del admin lee solo las pendientes, que son
    # una fracción minúscula del set total. Mantener el índice chico.
    op.create_index(
        "ix_categories_pending_review",
        "categories",
        ["slug"],
        postgresql_where=sa.text("pending_review = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_categories_pending_review",
        table_name="categories",
        postgresql_where=sa.text("pending_review = true"),
    )
    op.drop_column("categories", "pending_review")
