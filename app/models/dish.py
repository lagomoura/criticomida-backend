import enum
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
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

    # Relationships
    restaurant: Mapped["Restaurant"] = relationship(back_populates="dishes")  # noqa: F821
    reviews: Mapped[list["DishReview"]] = relationship(
        back_populates="dish", cascade="all, delete-orphan"
    )


class DishReview(Base):
    __tablename__ = "dish_reviews"
    __table_args__ = (
        UniqueConstraint("dish_id", "user_id", name="uq_dish_user_review"),
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
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    portion_size: Mapped[PortionSize | None] = mapped_column(
        Enum(PortionSize, name="portion_size"), nullable=True
    )
    would_order_again: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    visited_with: Mapped[str | None] = mapped_column(String(200), nullable=True)
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
