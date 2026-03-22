"""citext email, CHECK constraints, user_feedback

Revision ID: 003
Revises: 002
Create Date: 2026-03-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = '003'
down_revision: Union[str, None] = '002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS citext'))

    op.execute(
        sa.text('ALTER TABLE users ALTER COLUMN email TYPE citext USING email::citext')
    )

    op.create_check_constraint(
        'ck_dish_reviews_rating_1_5',
        'dish_reviews',
        'rating >= 1 AND rating <= 5',
    )
    op.create_check_constraint(
        'ck_restaurant_rating_dimensions_score_1_5',
        'restaurant_rating_dimensions',
        'score >= 1 AND score <= 5',
    )

    feedback_category = sa.Enum(
        'bug', 'feature', 'general',
        name='feedback_category',
        create_type=False,
    )
    feedback_category.create(op.get_bind(), checkfirst=True)
    feedback_status = sa.Enum(
        'open', 'read', 'closed',
        name='feedback_status',
        create_type=False,
    )
    feedback_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'user_feedback',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column(
            'user_id',
            UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column('category', feedback_category, nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column(
            'status',
            feedback_status,
            nullable=False,
            server_default=sa.text("'open'::feedback_status"),
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_user_feedback_user_id',
        'user_feedback',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_user_feedback_user_id', table_name='user_feedback')
    op.drop_table('user_feedback')
    sa.Enum(name='feedback_status').drop(op.get_bind(), checkfirst=True)
    sa.Enum(name='feedback_category').drop(op.get_bind(), checkfirst=True)

    op.drop_constraint(
        'ck_restaurant_rating_dimensions_score_1_5',
        'restaurant_rating_dimensions',
        type_='check',
    )
    op.drop_constraint(
        'ck_dish_reviews_rating_1_5',
        'dish_reviews',
        type_='check',
    )

    op.execute(
        sa.text(
            'ALTER TABLE users ALTER COLUMN email TYPE VARCHAR(255) '
            'USING email::text'
        )
    )
    op.execute(sa.text('DROP EXTENSION IF EXISTS citext'))
