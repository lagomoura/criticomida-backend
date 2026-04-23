import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)


class UserUpdate(BaseModel):
    """Legacy update schema used by admin endpoints."""

    display_name: str | None = Field(None, min_length=1, max_length=100)
    avatar_url: str | None = None


class UserProfileUpdate(BaseModel):
    """Payload for the logged-in user to edit their own public profile."""

    display_name: str | None = Field(None, min_length=1, max_length=100)
    handle: str | None = Field(
        None,
        min_length=3,
        max_length=30,
        pattern=r"^[a-zA-Z0-9_]+$",
    )
    bio: str | None = Field(None, max_length=500)
    location: str | None = Field(None, max_length=200)
    avatar_url: str | None = None


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None
    handle: str | None = None
    bio: str | None = None
    location: str | None = None
    role: UserRole
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserCounts(BaseModel):
    reviews: int = 0
    followers: int = 0
    following: int = 0


class PublicViewerState(BaseModel):
    """Describes the relationship between the caller and the profile owner.

    Populated only when the request carries a valid session; anonymous callers
    receive the default `False`s.
    """

    is_self: bool = False
    following: bool = False


class PublicUserResponse(BaseModel):
    """Public-facing profile consumed by /u/[id_or_handle]."""

    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    location: str | None = None
    counts: UserCounts
    viewer_state: PublicViewerState

    model_config = {"from_attributes": True}


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str | None = None
