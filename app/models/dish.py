import enum
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class PriceTier(str, enum.Enum):
    low = "$"
    mid = "$$"
    high = "$$$"


class PortionSize(str, enum.Enum):
    small = "small"
    medium = "medium"
    large = "large"


class DishReviewProsConsType(str, enum.Enum):
    pro = "pro"
    con = "con"


class Dish(Base):
    __tablename__ = "dishes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_normalized: Mapped[str] = mapped_column(
        Text,
        Computed("public.dish_name_normalized(name)", persisted=True),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    price_tier: Mapped[PriceTier | None] = mapped_column(
        Enum(PriceTier, name="price_tier"), nullable=True
    )
    computed_rating: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), default=Decimal("0"), nullable=False
    )
    review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    editorial_blurb: Mapped[str | None] = mapped_column(Text, nullable=True)
    editorial_blurb_lang: Mapped[str | None] = mapped_column(String(8), nullable=True)
    editorial_blurb_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    editorial_cached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    restaurant: Mapped["Restaurant"] = relationship(back_populates="dishes")  # noqa: F821
    reviews: Mapped[list["DishReview"]] = relationship(
        back_populates="dish", cascade="all, delete-orphan"
    )


class DishReview(Base):
    __tablename__ = "dish_reviews"
    __table_args__ = (
        CheckConstraint(
            "presentation IS NULL OR presentation BETWEEN 1 AND 3",
            name="ck_dish_reviews_presentation_range",
        ),
        CheckConstraint(
            "value_prop IS NULL OR value_prop BETWEEN 1 AND 3",
            name="ck_dish_reviews_value_prop_range",
        ),
        CheckConstraint(
            "execution IS NULL OR execution BETWEEN 1 AND 3",
            name="ck_dish_reviews_execution_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dish_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dishes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    date_tasted: Mapped[date] = mapped_column(Date, nullable=False)
    time_tasted: Mapped[time | None] = mapped_column(Time, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[Decimal] = mapped_column(Numeric(2, 1), nullable=False)
    portion_size: Mapped[PortionSize | None] = mapped_column(
        Enum(PortionSize, name="portion_size"), nullable=True
    )
    would_order_again: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    visited_with: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    presentation: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    value_prop: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    execution: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    dish: Mapped["Dish"] = relationship(back_populates="reviews")
    user: Mapped["User"] = relationship(back_populates="dish_reviews")  # noqa: F821
    pros_cons: Mapped[list["DishReviewProsCons"]] = relationship(
        back_populates="dish_review", cascade="all, delete-orphan"
    )
    tags: Mapped[list["DishReviewTag"]] = relationship(
        back_populates="dish_review", cascade="all, delete-orphan"
    )
    images: Mapped[list["DishReviewImage"]] = relationship(
        back_populates="dish_review", cascade="all, delete-orphan"
    )


class DishReviewProsCons(Base):
    __tablename__ = "dish_review_pros_cons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dish_review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dish_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[DishReviewProsConsType] = mapped_column(
        Enum(DishReviewProsConsType, name="dish_review_pros_cons_type"), nullable=False
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)

    # Relationships
    dish_review: Mapped["DishReview"] = relationship(back_populates="pros_cons")


class DishReviewTag(Base):
    __tablename__ = "dish_review_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dish_review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dish_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag: Mapped[str] = mapped_column(String(100), nullable=False)

    # Relationships
    dish_review: Mapped["DishReview"] = relationship(back_populates="tags")


class DishReviewImage(Base):
    __tablename__ = "dish_review_images"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dish_review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dish_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    alt_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    dish_review: Mapped["DishReview"] = relationship(back_populates="images")


class WantToTryDish(Base):
    """Wishlist row: user wants to try this dish. PK compuesta = uniqueness gratis."""

    __tablename__ = "want_to_try_dishes"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "dish_id", name="pk_want_to_try_dishes"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    dish_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dishes.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
