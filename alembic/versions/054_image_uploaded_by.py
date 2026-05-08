"""Track who uploaded each row in ``images``.

Today the ``images`` table doesn't record an uploader, which forces
``DELETE /api/images/{id}`` to gate on admin-only — a user cannot
remove their own dish photos. Sumadas a la falta de validación
previa de uploads, las imágenes huérfanas se acumulan sin control.

This migration adds ``uploaded_by_user_id`` (FK → ``users.id``) as
NULLABLE for two reasons:

1. Pre-existing rows were inserted without an actor, and we don't
   have a deterministic way to backfill them for every entity_type
   (e.g. ``restaurant_gallery``, ``menu``, ``chat_attachment``).
2. ``RestaurantOfficialPhoto.uploaded_by_user_id`` already follows
   this nullable shape, so the queries that union both sources
   stay symmetric.

The router will treat ``uploaded_by_user_id IS NULL`` as
"admin-only delete" — same fallback behaviour as today, no
regression for the legacy rows.

Indices: a partial index on ``WHERE uploaded_by_user_id IS NOT NULL``.
The "show me what user X uploaded" lookup matters; the count of
NULL rows is large but uninteresting for that query.

Backfill scope: ``dish_cover`` rows whose ``entity_id`` resolves to
a ``Dish`` with a known ``created_by`` are populated from there. It's
an approximation (the dish creator may not be the photo uploader),
but it lets the originating user delete their own dish photo at
least for the common case. Other entity_types (``restaurant_*``,
``menu``, ``chat_attachment``) keep ``NULL``.

Revision ID: 054
Revises: 053
Create Date: 2026-05-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "054"
down_revision: Union[str, None] = "053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "images",
        sa.Column(
            "uploaded_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Partial index: only rows we actually want to look up by uploader
    # are indexed. Keeps the index narrow on a table that may grow
    # large with NULL legacy rows.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_images_uploaded_by_user_id
            ON images (uploaded_by_user_id)
            WHERE uploaded_by_user_id IS NOT NULL;
        """
    )

    # Best-effort backfill for dish_cover rows. The ``dishes.created_by``
    # column is the closest thing to a creator we have for that path.
    # Other entity_types stay NULL — see the docstring for why.
    op.execute(
        """
        UPDATE images i
           SET uploaded_by_user_id = d.created_by
          FROM dishes d
         WHERE i.entity_type = 'dish_cover'
           AND i.entity_id = d.id
           AND i.uploaded_by_user_id IS NULL
           AND d.created_by IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_images_uploaded_by_user_id;")
    op.drop_column("images", "uploaded_by_user_id")
