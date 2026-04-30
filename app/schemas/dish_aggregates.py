"""Schemas for the enriched dish detail page (/dishes/[id])."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class ProsConsItem(BaseModel):
    text: str
    count: int


class TagItem(BaseModel):
    tag: str
    count: int


class WouldOrderAgainBreakdown(BaseModel):
    yes: int
    no: int
    no_answer: int
    pct: float | None = None


class PillarBreakdown(BaseModel):
    one: int
    two: int
    three: int
    answered: int
    avg: float | None = None


class PillarsAggregates(BaseModel):
    presentation: PillarBreakdown
    value_prop: PillarBreakdown
    execution: PillarBreakdown


class DishAggregatesResponse(BaseModel):
    pros_top: list[ProsConsItem]
    cons_top: list[ProsConsItem]
    tags_top: list[TagItem]
    rating_histogram: dict[str, int]
    portion_distribution: dict[str, int]
    would_order_again: WouldOrderAgainBreakdown
    pillars: PillarsAggregates
    photos_count: int
    unique_eaters: int


class DishPhotoItem(BaseModel):
    id: str
    url: str
    alt_text: str | None = None
    taken_at: datetime | None = None
    dish_id: uuid.UUID
    dish_name: str | None = None
    review_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    user_handle: str | None = None
    user_display_name: str | None = None
    is_cover: bool = False


class DishPhotosPage(BaseModel):
    items: list[DishPhotoItem]
    next_cursor: str | None = None


class RecentEater(BaseModel):
    id: uuid.UUID
    handle: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None


class FirstDiscoverer(BaseModel):
    """Uno de los 3 primeros reseñadores del plato (cronista fundador).

    `rank` 1 = el primer humano que dejó constancia del plato.
    `discovered_at` es el `created_at` de su reseña (no `date_tasted`, que es
    editable y rompería la idea de "quién llegó primero").
    """

    rank: Literal[1, 2, 3]
    user_id: uuid.UUID
    handle: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    discovered_at: datetime
    review_id: uuid.UUID


class DishDiaryStats(BaseModel):
    unique_eaters: int
    reviews_total: int
    reviews_last_7d: int
    recent_eaters: list[RecentEater]


class RelatedDishItem(BaseModel):
    id: uuid.UUID
    name: str
    cover_image_url: str | None = None
    computed_rating: Decimal
    review_count: int
    price_tier: Literal["$", "$$", "$$$"] | None = None
    restaurant_id: uuid.UUID
    restaurant_slug: str
    restaurant_name: str
    restaurant_location: str
    restaurant_city: str | None = None


class RelatedDishesResponse(BaseModel):
    items: list[RelatedDishItem]


class DishEditorialBlurb(BaseModel):
    blurb: str
    source: str
    lang: str | None = None
    cached_at: datetime | None = None


class DishTimelineBucket(BaseModel):
    """Resumen agregado de un período (trimestre o mes) en la evolución del plato.

    Los averages de pilares solo se calculan sobre reseñas que tienen el pilar
    seteado (no NULL); `review_count` es el total de reseñas del bucket
    independiente de los pilares.
    """

    period: str  # "2025-Q1" o "2025-03" según granularity
    review_count: int
    avg_rating: Decimal
    presentation_avg: float | None = None
    value_prop_avg: float | None = None
    execution_avg: float | None = None
    delta_rating: Decimal | None = None  # vs bucket anterior; None en el primero


class DishTimelineResponse(BaseModel):
    granularity: Literal["quarter", "month"]
    buckets: list[DishTimelineBucket]


class DishSocialDetailEnriched(BaseModel):
    """Enriched dish detail consumed by the new /dishes/[id] page."""

    id: uuid.UUID
    name: str
    description: str | None = None
    restaurant_id: uuid.UUID
    restaurant_name: str
    restaurant_slug: str
    restaurant_location_name: str | None = None
    restaurant_cover_url: str | None = None
    restaurant_average_rating: Decimal | None = None
    restaurant_google_rating: Decimal | None = None
    restaurant_latitude: Decimal | None = None
    restaurant_longitude: Decimal | None = None
    category: str | None = None
    cuisine_types: list[str] | None = None
    hero_image: str | None = None
    average_score: float
    review_count: int
    would_order_again_pct: float | None = None
    price_range: str | None = None
    is_signature: bool = False
    editorial_blurb: str | None = None
    editorial_source: str | None = None
    created_by_display_name: str | None = None
    want_to_try: bool = False
    first_discoverers: list[FirstDiscoverer] = []
