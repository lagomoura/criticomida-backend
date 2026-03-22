import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)


class UserUpdate(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=100)
    avatar_url: str | None = None


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None
    role: UserRole
    created_at: datetime
    updated_at: datetime

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
