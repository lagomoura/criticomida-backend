import uuid
from datetime import date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.dish import DishReviewProsConsType, PortionSize, PriceTier


class DishCreate(BaseModel):
    restaurant_id: uuid.UUID
    name: str = Field(max_length=200)
    description: str | None = None
    cover_image_url: str | None = None
    price_tier: PriceTier | None = None


class DishUpdate(BaseModel):
    name: str | None = Field(None, max_length=200)
    description: str | None = None
    cover_image_url: str | None = None
    price_tier: PriceTier | None = None


class DishResponse(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    name: str
    description: str | None
    cover_image_url: str | None
    price_tier: PriceTier | None
    computed_rating: Decimal
    review_count: int
    created_by: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class DishReviewProsConsCreate(BaseModel):
    type: DishReviewProsConsType
    text: str = Field(max_length=500)


class DishReviewProsConsResponse(BaseModel):
    id: int
    type: DishReviewProsConsType
    text: str

    model_config = {"from_attributes": True}


class DishReviewTagCreate(BaseModel):
    tag: str = Field(max_length=100)


class DishReviewTagResponse(BaseModel):
    id: int
    tag: str

    model_config = {"from_attributes": True}


class DishReviewImageCreate(BaseModel):
    url: str = Field(max_length=500)
    alt_text: str | None = Field(None, max_length=300)
    display_order: int = 0


class DishReviewImageResponse(BaseModel):
    id: uuid.UUID
    url: str
    alt_text: str | None
    display_order: int
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class DishReviewCreate(BaseModel):
    dish_id: uuid.UUID
    date_tasted: date
    time_tasted: time | None = None
    note: str
    rating: int = Field(ge=1, le=5)
    portion_size: PortionSize | None = None
    would_order_again: bool | None = None
    visited_with: str | None = Field(None, max_length=200)
    pros_cons: list[DishReviewProsConsCreate] = []
    tags: list[DishReviewTagCreate] = []
    images: list[DishReviewImageCreate] = []


class DishReviewUpdate(BaseModel):
    date_tasted: date | None = None
    time_tasted: time | None = None
    note: str | None = None
    rating: int | None = Field(None, ge=1, le=5)
    portion_size: PortionSize | None = None
    would_order_again: bool | None = None
    visited_with: str | None = Field(None, max_length=200)


class DishReviewResponse(BaseModel):
    id: uuid.UUID
    dish_id: uuid.UUID
    user_id: uuid.UUID
    user_display_name: str | None = None
    date_tasted: date
    time_tasted: time | None
    note: str
    rating: int
    portion_size: PortionSize | None
    would_order_again: bool | None
    visited_with: str | None
    created_at: datetime
    updated_at: datetime
    pros_cons: list[DishReviewProsConsResponse] = []
    tags: list[DishReviewTagResponse] = []
    images: list[DishReviewImageResponse] = []

    model_config = {"from_attributes": True}

    @model_validator(mode="wrap")
    @classmethod
    def _extract_user_display_name(cls, values, handler):  # type: ignore[no-untyped-def]
        result = handler(values)
        # Extract user display_name from the ORM relationship if available
        if hasattr(values, "user") and values.user is not None:
            result.user_display_name = values.user.display_name
        return result
