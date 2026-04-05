"""add otros category

Revision ID: 008
Revises: 007
Create Date: 2026-04-02
"""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO categories (slug, name, description, image_url, display_order)
        VALUES ('otros', 'Otros', 'Restaurantes sin categoría asignada', NULL, 99)
        ON CONFLICT (slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE slug = 'otros'")
