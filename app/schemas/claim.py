import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.restaurant_claim import ClaimStatus, VerificationMethod


class ClaimCreate(BaseModel):
    """POST /api/restaurants/{slug}/claims body."""

    verification_method: VerificationMethod
    contact_email: EmailStr | None = None
    evidence_urls: list[str] | None = Field(None, max_length=10)


class ClaimResponse(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    claimant_user_id: uuid.UUID
    status: ClaimStatus
    verification_method: VerificationMethod
    contact_email: str | None
    evidence_urls: list[str] | None
    submitted_at: datetime
    reviewed_at: datetime | None
    rejection_reason: str | None
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class ClaimListResponse(BaseModel):
    items: list[ClaimResponse]


class ClaimStatusResponse(BaseModel):
    """Light public endpoint — solo expone si hay owner verificado, sin
    identidad del owner para no doxear."""

    is_claimed: bool
