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


# ----- Admin schemas -----


class ClaimRestaurantSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    location_name: str
    is_claimed: bool


class ClaimClaimantSummary(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    role: str

    model_config = {"from_attributes": True}


class ClaimAdminResponse(BaseModel):
    """Hidratado para la vista admin: incluye snapshot del restaurant y del
    claimant para que el revisor decida sin fetches adicionales."""

    id: uuid.UUID
    status: ClaimStatus
    verification_method: VerificationMethod
    contact_email: str | None
    evidence_urls: list[str] | None
    submitted_at: datetime
    reviewed_at: datetime | None
    rejection_reason: str | None
    expires_at: datetime | None
    restaurant: ClaimRestaurantSummary
    claimant: ClaimClaimantSummary


class ClaimAdminListResponse(BaseModel):
    items: list[ClaimAdminResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ClaimApproveBody(BaseModel):
    notes: str | None = Field(None, max_length=2000)


class ClaimRejectBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class ClaimRevokeBody(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)
