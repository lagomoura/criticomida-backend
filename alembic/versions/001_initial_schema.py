"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Enum types (create_type=False prevents double-creation by create_table) ---
    user_role = sa.Enum("admin", "critic", "user", name="user_role", create_type=False)
    user_role.create(op.get_bind(), checkfirst=True)

    entity_type = sa.Enum(
        "restaurant_cover", "restaurant_gallery", "dish_cover", "menu",
        name="entity_type", create_type=False,
    )
    entity_type.create(op.get_bind(), checkfirst=True)

    rating_dimension = sa.Enum(
        "cleanliness", "ambiance", "service", "value", "food_quality",
        name="rating_dimension", create_type=False,
    )
    rating_dimension.create(op.get_bind(), checkfirst=True)

    pros_cons_type = sa.Enum("pro", "con", name="pros_cons_type", create_type=False)
    pros_cons_type.create(op.get_bind(), checkfirst=True)

    price_tier = sa.Enum("$", "$$", "$$$", name="price_tier", create_type=False)
    price_tier.create(op.get_bind(), checkfirst=True)

    portion_size = sa.Enum("small", "medium", "large", name="portion_size", create_type=False)
    portion_size.create(op.get_bind(), checkfirst=True)

    dish_review_pros_cons_type = sa.Enum("pro", "con", name="dish_review_pros_cons_type", create_type=False)
    dish_review_pros_cons_type.create(op.get_bind(), checkfirst=True)

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("role", user_role, nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- categories ---
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(100), unique=True, nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("image_url", sa.String(500), nullable=True),
        sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
    )

    # --- images (standalone polymorphic) ---
    op.create_table(
        "images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", entity_type, nullable=False, index=True),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("alt_text", sa.String(300), nullable=True),
        sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- restaurants ---
    op.create_table(
        "restaurants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(200), unique=True, nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("location_name", sa.String(300), nullable=False),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id"), nullable=False, index=True),
        sa.Column("cover_image_url", sa.String(500), nullable=True),
        sa.Column("computed_rating", sa.Numeric(3, 2), nullable=False, server_default="0"),
        sa.Column("review_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- restaurant_rating_dimensions ---
    op.create_table(
        "restaurant_rating_dimensions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("restaurant_id", UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("dimension", rating_dimension, nullable=False),
        sa.Column("score", sa.Numeric(2, 1), nullable=False),
        sa.UniqueConstraint("restaurant_id", "user_id", "dimension", name="uq_rest_user_dimension"),
    )

    # --- restaurant_pros_cons ---
    op.create_table(
        "restaurant_pros_cons",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("restaurant_id", UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", pros_cons_type, nullable=False),
        sa.Column("text", sa.String(500), nullable=False),
    )

    # --- visit_diary_entries ---
    op.create_table(
        "visit_diary_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("restaurant_id", UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("visit_date", sa.Date, nullable=False),
        sa.Column("diary_text", sa.Text, nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- menus ---
    op.create_table(
        "menus",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("restaurant_id", UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), unique=True, nullable=False),
        sa.Column("image_url", sa.String(500), nullable=False),
        sa.Column("upload_date", sa.Date, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- dishes ---
    op.create_table(
        "dishes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("cover_image_url", sa.String(500), nullable=True),
        sa.Column("price_tier", price_tier, nullable=True),
        sa.Column("computed_rating", sa.Numeric(3, 2), nullable=False, server_default="0"),
        sa.Column("review_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- dish_reviews ---
    op.create_table(
        "dish_reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dish_id", UUID(as_uuid=True), sa.ForeignKey("dishes.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("date_tasted", sa.Date, nullable=False),
        sa.Column("time_tasted", sa.Time, nullable=True),
        sa.Column("note", sa.Text, nullable=False),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("portion_size", portion_size, nullable=True),
        sa.Column("would_order_again", sa.Boolean, nullable=True),
        sa.Column("visited_with", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dish_id", "user_id", name="uq_dish_user_review"),
    )

    # --- dish_review_pros_cons ---
    op.create_table(
        "dish_review_pros_cons",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("dish_review_id", UUID(as_uuid=True), sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("type", dish_review_pros_cons_type, nullable=False),
        sa.Column("text", sa.String(500), nullable=False),
    )

    # --- dish_review_tags ---
    op.create_table(
        "dish_review_tags",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("dish_review_id", UUID(as_uuid=True), sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("tag", sa.String(100), nullable=False),
    )

    # --- dish_review_images ---
    op.create_table(
        "dish_review_images",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("dish_review_id", UUID(as_uuid=True), sa.ForeignKey("dish_reviews.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("alt_text", sa.String(300), nullable=True),
        sa.Column("display_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dish_review_images")
    op.drop_table("dish_review_tags")
    op.drop_table("dish_review_pros_cons")
    op.drop_table("dish_reviews")
    op.drop_table("dishes")
    op.drop_table("menus")
    op.drop_table("visit_diary_entries")
    op.drop_table("restaurant_pros_cons")
    op.drop_table("restaurant_rating_dimensions")
    op.drop_table("restaurants")
    op.drop_table("images")
    op.drop_table("categories")
    op.drop_table("users")

    sa.Enum(name="dish_review_pros_cons_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="portion_size").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="price_tier").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="pros_cons_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="rating_dimension").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="entity_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)
