"""restaurant_reservations — afiliados de reservas

Agrega 3 columnas a `restaurants` (URL externa, provider, meta JSONB) y crea
la tabla `reservation_clicks` para tracking de clicks (medir CTR del CTA antes
de invertir más en el pilar B2B).

Revision ID: 023
Revises: 022
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "restaurants",
        sa.Column("reservation_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column("reservation_provider", sa.String(32), nullable=True),
    )
    op.add_column(
        "restaurants",
        sa.Column(
            "reservation_partner_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    op.create_table(
        "reservation_clicks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "restaurant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column(
            "clicked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column(
            "utm",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("session_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_reservation_clicks_restaurant_clicked_at",
        "reservation_clicks",
        ["restaurant_id", sa.text("clicked_at DESC")],
    )
    op.create_index(
        "ix_reservation_clicks_session",
        "reservation_clicks",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reservation_clicks_session", table_name="reservation_clicks"
    )
    op.drop_index(
        "ix_reservation_clicks_restaurant_clicked_at",
        table_name="reservation_clicks",
    )
    op.drop_table("reservation_clicks")
    op.drop_column("restaurants", "reservation_partner_meta")
    op.drop_column("restaurants", "reservation_provider")
    op.drop_column("restaurants", "reservation_url")
