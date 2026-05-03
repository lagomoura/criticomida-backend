import enum
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum, String
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    admin = "admin"
    critic = "critic"
    user = "user"


class Gender(str, enum.Enum):
    female = "female"
    male = "male"
    non_binary = "non_binary"
    prefer_not_to_say = "prefer_not_to_say"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        CITEXT, unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    handle: Mapped[str | None] = mapped_column(CITEXT, unique=True, nullable=True)
    bio: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.user, nullable=False
    )
    gender: Mapped[Gender | None] = mapped_column(
        Enum(Gender, name="user_gender"), nullable=True
    )
    birth_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
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
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    # Relationships
    restaurants: Mapped[list["Restaurant"]] = relationship(  # noqa: F821
        back_populates="creator", foreign_keys="Restaurant.created_by"
    )
    dish_reviews: Mapped[list["DishReview"]] = relationship(  # noqa: F821
        back_populates="user"
    )
    dimension_ratings: Mapped[list["RestaurantRatingDimension"]] = relationship(  # noqa: F821
        back_populates="user"
    )
    feedback_submissions: Mapped[list["UserFeedback"]] = relationship(  # noqa: F821
        back_populates="user"
    )
