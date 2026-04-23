import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RatingDimension(str, enum.Enum):
    cleanliness = "cleanliness"
    ambiance = "ambiance"
    service = "service"
    value = "value"
    food_quality = "food_quality"


class ProsConsType(str, enum.Enum):
    pro = "pro"
    con = "con"


class Restaurant(Base):
    __tablename__ = "restaurants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(200), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_name: Mapped[str] = mapped_column(String(300), nullable=False)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 7), nullable=True
    )
    longitude: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 7), nullable=True
    )
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id"), nullable=True, index=True
    )
    cover_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_place_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    google_maps_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_level: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    opening_hours: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    category: Mapped["Category | None"] = relationship(back_populates="restaurants")  # noqa: F821
    creator: Mapped["User"] = relationship(  # noqa: F821
        back_populates="restaurants", foreign_keys=[created_by]
    )
    dishes: Mapped[list["Dish"]] = relationship(back_populates="restaurant")  # noqa: F821
    dimension_ratings: Mapped[list["RestaurantRatingDimension"]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    pros_cons: Mapped[list["RestaurantProsCons"]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    diary_entries: Mapped[list["VisitDiaryEntry"]] = relationship(
        back_populates="restaurant", cascade="all, delete-orphan"
    )
    menu: Mapped["Menu | None"] = relationship(  # noqa: F821
        back_populates="restaurant", uselist=False
    )


class RestaurantRatingDimension(Base):
    __tablename__ = "restaurant_rating_dimensions"
    __table_args__ = (
        UniqueConstraint("restaurant_id", "user_id", "dimension", name="uq_rest_user_dimension"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    dimension: Mapped[RatingDimension] = mapped_column(
        Enum(RatingDimension, name="rating_dimension"), nullable=False
    )
    score: Mapped[Decimal] = mapped_column(Numeric(2, 1), nullable=False)

    # Relationships
    restaurant: Mapped["Restaurant"] = relationship(back_populates="dimension_ratings")
    user: Mapped["User"] = relationship(back_populates="dimension_ratings")  # noqa: F821


class RestaurantProsCons(Base):
    __tablename__ = "restaurant_pros_cons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    type: Mapped[ProsConsType] = mapped_column(
        Enum(ProsConsType, name="pros_cons_type"), nullable=False
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)

    # Relationships
    restaurant: Mapped["Restaurant"] = relationship(back_populates="pros_cons")


class VisitDiaryEntry(Base):
    __tablename__ = "visit_diary_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    visit_date: Mapped[date] = mapped_column(Date, nullable=False)
    diary_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    restaurant: Mapped["Restaurant"] = relationship(back_populates="diary_entries")
