import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.image import EntityType


class ImageCreate(BaseModel):
    entity_type: EntityType
    entity_id: uuid.UUID
    url: str = Field(max_length=500)
    alt_text: str | None = Field(None, max_length=300)
    display_order: int = 0


class ImageResponse(BaseModel):
    id: uuid.UUID
    entity_type: EntityType
    entity_id: uuid.UUID
    url: str
    alt_text: str | None
    display_order: int
    uploaded_at: datetime

    model_config = {"from_attributes": True}
