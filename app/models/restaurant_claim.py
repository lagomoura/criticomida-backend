import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column


class ClaimStatus(str, enum.Enum):
    pending = "pending"
    verifying = "verifying"
    verified = "verified"
    rejected = "rejected"
    revoked = "revoked"


class VerificationMethod(str, enum.Enum):
    domain_email = "domain_email"
    google_business = "google_business"
    manual_admin = "manual_admin"
    phone_callback = "phone_callback"


# Modelado como String laxo en DB (sin Postgres Enum) para iterar valores sin
# migración. La validación Pydantic en schemas se encarga de los valores
# admitidos.

from app.database import Base


class RestaurantClaim(Base):
    __tablename__ = "restaurant_claims"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    claimant_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=ClaimStatus.pending.value, nullable=False
    )
    verification_method: Mapped[str] = mapped_column(String(24), nullable=False)
    verification_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    evidence_urls: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
