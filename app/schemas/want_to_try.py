"""Schemas for the dish wishlist ('Quiero probarlo')."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class WantToTryActionResponse(BaseModel):
    dish_id: uuid.UUID
    want_to_try: bool


class WantToTryItem(BaseModel):
    dish_id: uuid.UUID
    dish_name: str
    cover_image_url: str | None = None
    computed_rating: Decimal
    review_count: int
    restaurant_id: uuid.UUID
    restaurant_slug: str
    restaurant_name: str
    restaurant_city: str | None = None
    restaurant_latitude: Decimal | None = None
    restaurant_longitude: Decimal | None = None
    saved_at: datetime


class WantToTryPage(BaseModel):
    items: list[WantToTryItem]
    next_cursor: str | None = None
