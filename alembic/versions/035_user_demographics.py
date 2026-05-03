"""Optional demographics on users: gender + birth_date.

Both columns are nullable. Recolection happens opt-in from the profile
page (``/[locale]/settings``), never on signup. The owner dashboard
exposes only a derived ``age_range`` bucket — the raw ``birth_date``
never leaves the API.

Revision ID: 035
Revises: 034
Create Date: 2026-05-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGEnum


revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


user_gender_t = PGEnum(
    "female", "male", "non_binary", "prefer_not_to_say",
    name="user_gender", create_type=False,
)


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE user_gender AS ENUM (
                'female', 'male', 'non_binary', 'prefer_not_to_say'
            );
        EXCEPTION WHEN duplicate_object THEN
            null;
        END $$;
        """
    )

    op.add_column(
        "users",
        sa.Column("gender", user_gender_t, nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("birth_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "birth_date")
    op.drop_column("users", "gender")
    op.execute("DROP TYPE IF EXISTS user_gender")
