import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.dish import PortionSize, SentimentLabel
from app.models.user import Gender, UserRole


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
    rápido cuáles requieren atención sin un fetch extra por reseña, y el
    sentimiento detectado para priorizar las negativas. ``sentiment_*``
    es interno: nunca se expone en la vista pública de la reseña.

    Los campos ``author_*`` se omiten (None) cuando ``is_anonymous`` es
    True para respetar end-to-end la decisión del cliente de reseñar
    de forma anónima. ``author_age_range`` es un bucket derivado de
    ``birth_date`` — la fecha exacta nunca sale del backend.
    """

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
    sentiment_label: SentimentLabel | None = None
    sentiment_score: float | None = None
    presentation: int | None = None
    execution: int | None = None
    value_prop: int | None = None
    portion_size: PortionSize | None = None
    would_order_again: bool | None = None
    author_role: UserRole | None = None
    author_gender: Gender | None = None
    author_age_range: str | None = None


class OwnerReviewsListResponse(BaseModel):
    items: list[OwnerReviewItem]
    total: int
    pending_count: int
