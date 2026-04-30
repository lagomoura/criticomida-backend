import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ----- Dish review owner response -----


class OwnerResponseUpsert(BaseModel):
    body: str = Field(min_length=3, max_length=2000)


class OwnerResponseRead(BaseModel):
    review_id: uuid.UUID
    owner_user_id: uuid.UUID | None
    body: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ----- Restaurant official photo -----


class OfficialPhotoCreate(BaseModel):
    """JSON-only create body. La subida del archivo se hace previamente vía
    /api/images/upload (que devuelve una URL en /uploads/…); acá solo se
    asocia esa URL al restaurant como foto oficial. Mantiene el endpoint
    simple y reusa el storage existente."""

    url: str = Field(min_length=1, max_length=500)
    alt_text: str | None = Field(None, max_length=300)
    display_order: int = 0


class OfficialPhotoRead(BaseModel):
    id: uuid.UUID
    restaurant_id: uuid.UUID
    url: str
    alt_text: str | None
    display_order: int
    uploaded_by_user_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class OfficialPhotosListResponse(BaseModel):
    items: list[OfficialPhotoRead]


# ----- Owner dashboard -----


class OwnerReviewItem(BaseModel):
    """Vista plana de cada reseña del restaurant para el dashboard del dueño.

    Incluye el flag has_owner_response para que el frontend pueda destacar
    rápido cuáles requieren atención sin un fetch extra por reseña."""

    id: uuid.UUID
    dish_id: uuid.UUID
    dish_name: str
    rating: float
    note: str
    user_display_name: str
    user_handle: str | None = None
    is_anonymous: bool
    date_tasted: str
    has_owner_response: bool


class OwnerReviewsListResponse(BaseModel):
    items: list[OwnerReviewItem]
    total: int
    pending_count: int
