"""Input schema for POST /api/posts (social compose flow)."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PostCreateExtras(BaseModel):
    portion_size: Literal["small", "medium", "large"] | None = None
    would_order_again: bool | None = None
    visited_with: str | None = Field(default=None, max_length=200)
    is_anonymous: bool | None = None
    date_tasted: date | None = None
    price_tier: Literal["$", "$$", "$$$"] | None = None
    pros: list[str] = Field(default_factory=list, max_length=20)
    cons: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    presentation: int | None = Field(default=None, ge=1, le=3)
    value_prop: int | None = Field(default=None, ge=1, le=3)
    execution: int | None = Field(default=None, ge=1, le=3)


class RestaurantFromPlace(BaseModel):
    """Restaurant identified via a Google Places `place_id`.

    The frontend Places Autocomplete resolves the user's pick and sends us the
    trusted identifier plus the denormalized fields Google returned. The
    backend dedupes by `place_id` and only creates a new row when unseen.
    """

    place_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    formatted_address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    city: str | None = Field(default=None, max_length=100)
    google_maps_url: str | None = Field(default=None, max_length=500)
    website: str | None = Field(default=None, max_length=500)
    phone_number: str | None = Field(default=None, max_length=50)


class PostCreate(BaseModel):
    # New Places-sourced path (preferred).
    restaurant: RestaurantFromPlace | None = None
    # Legacy free-text path — kept for backward compatibility with existing
    # callers (mocks, scripts). Real user-facing flows must send `restaurant`.
    restaurant_name: str | None = Field(default=None, min_length=1, max_length=200)

    dish_name: str = Field(min_length=1, max_length=200)
    # Optional: when the frontend picked an existing dish from autocomplete,
    # skip find-or-create-by-name and use this row directly. Backend verifies
    # the dish belongs to the resolved restaurant before trusting it.
    dish_id: uuid.UUID | None = None
    category: str | None = None
    score: Decimal = Field(ge=1, le=5, decimal_places=1)
    text: str = Field(min_length=1, max_length=2000)
    extras: PostCreateExtras | None = None

    @model_validator(mode="after")
    def _require_one_restaurant_source(self) -> "PostCreate":
        if self.restaurant is None and not self.restaurant_name:
            raise ValueError(
                "Falta la información del restaurante (`restaurant` con "
                "`place_id` o, en legacy, `restaurant_name`)."
            )
        return self
