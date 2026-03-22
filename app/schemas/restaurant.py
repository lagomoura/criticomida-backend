import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.restaurant import ProsConsType, RatingDimension
from app.schemas.category import CategoryResponse
from app.schemas.user import UserResponse


class RestaurantCreate(BaseModel):
    slug: str = Field(max_length=200)
    name: str = Field(max_length=200)
    description: str | None = None
    location_name: str = Field(max_length=300)
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    category_id: int
    cover_image_url: str | None = None


class RestaurantUpdate(BaseModel):
    slug: str | None = Field(None, max_length=200)
    name: str | None = Field(None, max_length=200)
    description: str | None = None
    location_name: str | None = Field(None, max_length=300)
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    category_id: int | None = None
    cover_image_url: str | None = None


class RestaurantResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    location_name: str
    latitude: Decimal | None
    longitude: Decimal | None
    category_id: int
    cover_image_url: str | None
    computed_rating: Decimal
    review_count: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    category: CategoryResponse | None = None
    creator: UserResponse | None = None

    model_config = {"from_attributes": True}


class RestaurantListResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    location_name: str
    cover_image_url: str | None
    computed_rating: Decimal
    review_count: int
    category: CategoryResponse | None = None

    model_config = {"from_attributes": True}


class RatingDimensionCreate(BaseModel):
    dimension: RatingDimension
    score: Decimal = Field(ge=1, le=5)


class RatingDimensionResponse(BaseModel):
    id: int
    restaurant_id: uuid.UUID
    user_id: uuid.UUID
    dimension: RatingDimension
    score: Decimal

    model_config = {"from_attributes": True}


class ProsConsCreate(BaseModel):
    type: ProsConsType
    text: str = Field(max_length=500)


class ProsConsResponse(BaseModel):
    id: int
    restaurant_id: uuid.UUID
    user_id: uuid.UUID
    type: ProsConsType
    text: str

    model_config = {"from_attributes": True}


class VisitDiaryEntryCreate(BaseModel):
    visit_date: date
    diary_text: str


class VisitDiaryEntryResponse(BaseModel):
    id: int
    restaurant_id: uuid.UUID
    visit_date: date
    diary_text: str
    created_by: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}
