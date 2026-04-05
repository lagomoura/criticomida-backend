"""change cover_image_url, website, google_maps_url to TEXT

Revision ID: 006
Revises: 005
Create Date: 2026-04-02

Google Maps photo URLs can exceed 500 characters due to session tokens and
query parameters. Using TEXT removes the length constraint.
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("restaurants", "cover_image_url",
                    existing_type=sa.String(500), type_=sa.Text(), existing_nullable=True)
    op.alter_column("restaurants", "website",
                    existing_type=sa.String(500), type_=sa.Text(), existing_nullable=True)
    op.alter_column("restaurants", "google_maps_url",
                    existing_type=sa.String(500), type_=sa.Text(), existing_nullable=True)


def downgrade() -> None:
    op.alter_column("restaurants", "google_maps_url",
                    existing_type=sa.Text(), type_=sa.String(500), existing_nullable=True)
    op.alter_column("restaurants", "website",
                    existing_type=sa.Text(), type_=sa.String(500), existing_nullable=True)
    op.alter_column("restaurants", "cover_image_url",
                    existing_type=sa.Text(), type_=sa.String(500), existing_nullable=True)
