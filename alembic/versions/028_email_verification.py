"""Email verification post-signup (no-bloqueante)

Agrega users.email_verified_at y la tabla email_verification_tokens. El
token se guarda hasheado (SHA-256) — el plain solo viaja en el email.

Revision ID: 028
Revises: 027
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_email_verification_tokens_user",
        "email_verification_tokens",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verification_tokens_user",
        table_name="email_verification_tokens",
    )
    op.drop_table("email_verification_tokens")
    op.drop_column("users", "email_verified_at")
