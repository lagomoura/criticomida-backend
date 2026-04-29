import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.restaurant import ProsConsType, RatingDimension
from app.schemas.category import CategoryResponse
from app.schemas.user import UserResponse


class ProsConsAggregateItem(BaseModel):
    text: str
    count: int


class DimensionAggregate(BaseModel):
    average: Decimal | None
    count: int


class RestaurantAggregatesResponse(BaseModel):
    pros_top: list[ProsConsAggregateItem]
    cons_top: list[ProsConsAggregateItem]
    dimension_averages: dict[str, DimensionAggregate]
    photos_count: int
    dishes_count: int
    reviews_count: int


class RestaurantPhotoItem(BaseModel):
    id: uuid.UUID
    url: str
    alt_text: str | None = None
    taken_at: datetime
    dish_id: uuid.UUID
    dish_name: str
    review_id: uuid.UUID | None = None
    user_id: uuid.UUID
    user_handle: str | None = None
    user_display_name: str


class RestaurantPhotosResponse(BaseModel):
    items: list[RestaurantPhotoItem]
    next_cursor: str | None = None


class DiaryVisitor(BaseModel):
    id: uuid.UUID
    handle: str | None = None
    display_name: str
    avatar_url: str | None = None


class MostOrderedDish(BaseModel):
    id: uuid.UUID
    name: str
    review_count: int


class DiaryStatsResponse(BaseModel):
    unique_visitors: int
    visits_total: int
    visits_last_7d: int
    most_ordered_dish: MostOrderedDish | None = None
    recent_visitors: list[DiaryVisitor]


class SignatureDishItem(BaseModel):
    id: uuid.UUID
    name: str
    cover_image_url: str | None = None
    computed_rating: Decimal
    review_count: int
    best_quote: str | None = None
    best_quote_author: str | None = None


class SignatureDishesResponse(BaseModel):
    items: list[SignatureDishItem]


class NearbyRestaurantItem(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    location_name: str
    cover_image_url: str | None = None
    google_photo_url: str | None = None
    computed_rating: Decimal
    review_count: int
    category: CategoryResponse | None = None
    distance_km: float


class NearbyRestaurantsResponse(BaseModel):
    items: list[NearbyRestaurantItem]


class RestaurantCreate(BaseModel):
    slug: str = Field(max_length=200)
    name: str = Field(max_length=200)
    description: str | None = None
    location_name: str = Field(max_length=300)
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    category_id: int | None = None
    cover_image_url: str | None = None
    google_place_id: str | None = Field(None, max_length=200)
    website: str | None = None
    phone_number: str | None = Field(None, max_length=50)
    google_maps_url: str | None = None
    price_level: int | None = None
    opening_hours: list[str] | None = None


class RestaurantUpdate(BaseModel):
    slug: str | None = Field(None, max_length=200)
    name: str | None = Field(None, max_length=200)
    description: str | None = None
    location_name: str | None = Field(None, max_length=300)
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    category_id: int | None = None
    cover_image_url: str | None = None
    google_place_id: str | None = Field(None, max_length=200)
    website: str | None = None
    phone_number: str | None = Field(None, max_length=50)
    google_maps_url: str | None = None
    price_level: int | None = None
    opening_hours: list[str] | None = None


class RestaurantResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    location_name: str
    latitude: Decimal | None
    longitude: Decimal | None
    category_id: int | None
    cover_image_url: str | None
    computed_rating: Decimal
    review_count: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    category: CategoryResponse | None = None
    creator: UserResponse | None = None
    google_place_id: str | None = None
    website: str | None = None
    phone_number: str | None = None
    google_maps_url: str | None = None
    price_level: int | None = None
    opening_hours: list[str] | None = None
    # Fase B — Google Places enrichment
    google_rating: Decimal | None = None
    google_user_ratings_total: int | None = None
    google_photos: list[dict] | None = None
    editorial_summary: str | None = None
    editorial_summary_lang: str | None = None
    cuisine_types: list[str] | None = None
    google_cached_at: datetime | None = None

    model_config = {"from_attributes": True}


class RestaurantCreateResponse(RestaurantResponse):
    """Response for POST /api/restaurants.

    `existed` is True when a row with the same `google_place_id` was found and
    returned instead of creating a new one. Lets the frontend route the user to
    the existing restaurant instead of showing a "created" toast.
    """
    existed: bool = False


class RestaurantListResponse(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    location_name: str
    latitude: Decimal | None = None
    longitude: Decimal | None = None
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
