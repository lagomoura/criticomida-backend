import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class MenuCreate(BaseModel):
    restaurant_id: uuid.UUID
    image_url: str = Field(max_length=500)
    upload_date: date


class MenuUpdate(BaseModel):
    image_url: str | None = Field(None, max_length=500)
    upload_date: date | None = None


class MenuResponse(BaseModel):
    id: int
    restaurant_id: uuid.UUID
    image_url: str
    upload_date: date
    updated_at: datetime

    model_config = {"from_attributes": True}
