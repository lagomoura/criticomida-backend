"""Phase 1 of the agentic chatbot: curated dish lists ("rutas").

Tables:
- ``dish_lists`` — owner, unique slug, public flag, optional link to
  the chat conversation that spawned it.
- ``dish_list_items`` — composite PK on (list_id, dish_id), ordered by
  ``position``.

Revision ID: 032
Revises: 031
Create Date: 2026-05-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dish_lists",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "source_conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_dish_lists_owner_created",
        "dish_lists",
        ["owner_user_id", "created_at"],
    )
    op.create_index("ix_dish_lists_slug", "dish_lists", ["slug"])

    op.create_table(
        "dish_list_items",
        sa.Column(
            "list_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dish_lists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dish_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dishes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "list_id", "dish_id", name="pk_dish_list_items"
        ),
    )
    op.create_index(
        "ix_dish_list_items_list_position",
        "dish_list_items",
        ["list_id", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dish_list_items_list_position", table_name="dish_list_items"
    )
    op.drop_table("dish_list_items")
    op.drop_index("ix_dish_lists_slug", table_name="dish_lists")
    op.drop_index("ix_dish_lists_owner_created", table_name="dish_lists")
    op.drop_table("dish_lists")
