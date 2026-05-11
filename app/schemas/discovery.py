"""Schemas para el feed de descubrimiento (Geek Score, rails y duelo)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


PillarKey = Literal["value_prop", "execution", "presentation", "overall_rating"]
"""Pilares por los que se puede duelar un par de platos.

- `value_prop` / `execution` / `presentation`: pilares técnicos 1..3 cargados
  por reseña (avg con shrinkage bayesiano server-side).
- `overall_rating`: rating general 1..5 (también shrunk).
"""

DuelFallbackReason = Literal[
    "root_unique_restaurant",
    "root_not_found",
    "family_unique_restaurant",
    "family_not_found",
]


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
    """Top 2 platos enfrentados por un pilar elegido.

    Modos de scope:
    - `family`: 2 platos de restaurantes distintos cuyo `dish_root` pertenece
      a la misma familia (burger, pizza, pasta, ...). Default del rail nuevo.
    - `root`: 2 platos con la MISMA `dish_root` exacta (más restrictivo).
    - solo `category`: legacy, top 2 de la categoría del restaurante.
    """

    category: str | None = None
    root: str | None = None
    family: str | None = None
    pillar: PillarKey | None = None
    items: list[DiscoveryDishItem]
    fallback_reason: DuelFallbackReason | None = None


class DuelRootItem(BaseModel):
    """Raíz semántica con al menos `min_restaurants` contendientes."""

    root: str
    restaurant_count: int
    recent_reviews: int
    sample_name: str


class DuelRootsResponse(BaseModel):
    items: list[DuelRootItem]


class DuelFamilyItem(BaseModel):
    """Familia semántica con al menos `min_restaurants` contendientes."""

    family: str
    restaurant_count: int
    recent_reviews: int
    sample_name: str


class DuelFamiliesResponse(BaseModel):
    items: list[DuelFamilyItem]


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
