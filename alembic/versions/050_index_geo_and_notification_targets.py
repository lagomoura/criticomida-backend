"""Hot-path indexes: restaurants(lat, lng) + notifications.target_*_id.

Audit-driven: ALTO #4 y MEDIO #6 del audit DB de 2026-05-08.

- ``ix_restaurants_lat_lng``: bbox queries del search del Sommelier
  (``tools/search.py``) y del benchmark del Business
  (``tools/business.py``) filtran ``latitude.between(...) AND
  longitude.between(...)`` sin índice. Partial: solo rows con coordenadas.
- ``ix_notifications_target_*``: Postgres no crea índice automático
  del lado hijo de una FK. Cada DELETE de ``users``, ``dish_reviews``,
  ``restaurants`` o ``comments`` dispara ON DELETE CASCADE sobre
  ``notifications`` con seq scan. Partials porque cada row solo usa uno
  de los 4 ``target_*``.

Revision ID: 050
Revises: 049
Create Date: 2026-05-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "050"
down_revision: Union[str, None] = "049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_restaurants_lat_lng",
        "restaurants",
        ["latitude", "longitude"],
        postgresql_where=sa.text(
            "latitude IS NOT NULL AND longitude IS NOT NULL"
        ),
    )

    op.create_index(
        "ix_notifications_target_review_id",
        "notifications",
        ["target_review_id"],
        postgresql_where=sa.text("target_review_id IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_target_user_id",
        "notifications",
        ["target_user_id"],
        postgresql_where=sa.text("target_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_target_restaurant_id",
        "notifications",
        ["target_restaurant_id"],
        postgresql_where=sa.text("target_restaurant_id IS NOT NULL"),
    )
    op.create_index(
        "ix_notifications_target_comment_id",
        "notifications",
        ["target_comment_id"],
        postgresql_where=sa.text("target_comment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_target_comment_id", table_name="notifications")
    op.drop_index("ix_notifications_target_restaurant_id", table_name="notifications")
    op.drop_index("ix_notifications_target_user_id", table_name="notifications")
    op.drop_index("ix_notifications_target_review_id", table_name="notifications")
    op.drop_index("ix_restaurants_lat_lng", table_name="restaurants")
