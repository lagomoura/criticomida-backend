"""Schemas para el feed de descubrimiento (Geek Score, rails y duelo)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel


class DiscoveryPillarStats(BaseModel):
    """Promedios por pilar a nivel plato + cuántas reseñas respondieron cada pilar."""

    presentation_avg: float | None = None
    presentation_n: int = 0
    value_prop_avg: float | None = None
    value_prop_n: int = 0
    execution_avg: float | None = None
    execution_n: int = 0


class DiscoveryDishItem(BaseModel):
    dish_id: uuid.UUID
    dish_name: str
    cover_image_url: str | None = None
    price_tier: str | None = None
    computed_rating: Decimal
    review_count: int
    geek_score: float  # 0..100
    pillars: DiscoveryPillarStats
    distance_km: float | None = None
    restaurant_id: uuid.UUID
    restaurant_slug: str
    restaurant_name: str
    restaurant_city: str | None = None
    category: str | None = None
    want_to_try: bool = False


class DiscoveryDishPage(BaseModel):
    items: list[DiscoveryDishItem]


class DishDuelResponse(BaseModel):
    """Top 2 platos de una categoría rankeados por costo/beneficio."""

    category: str | None = None
    items: list[DiscoveryDishItem]


class MapDishHighlight(BaseModel):
    dish_id: uuid.UUID
    name: str
    cover_image_url: str | None = None
    execution_avg: float | None = None
    value_prop_avg: float | None = None
    presentation_avg: float | None = None
    review_count: int
    geek_score: float


class MapRestaurantPin(BaseModel):
    restaurant_id: uuid.UUID
    slug: str
    name: str
    latitude: float
    longitude: float
    top_geek_score: float
    has_chef_badge: bool
    has_gem_badge: bool
    cover_image_url: str | None = None
    location_name: str | None = None
    computed_rating: float
    review_count: int
    price_level: int | None = None
    cuisine_types: list[str] | None = None
    category_name: str | None = None
    trending_count: int = 0
    is_empty: bool = False
    golden_dish: MapDishHighlight | None = None
    best_value_dish: MapDishHighlight | None = None


class MapBboxResponse(BaseModel):
    items: list[MapRestaurantPin]
    truncated: bool
