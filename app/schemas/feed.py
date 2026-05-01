"""Feed and review-detail schemas consumed by the social UI."""

import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel


class FeedAuthor(BaseModel):
    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None


class FeedDish(BaseModel):
    id: uuid.UUID
    name: str
    restaurant_id: uuid.UUID
    restaurant_name: str
    category: str | None = None


class FeedMediaImage(BaseModel):
    url: str
    alt: str | None = None


class FeedStats(BaseModel):
    likes: int = 0
    comments: int = 0
    saves: int = 0


class FeedViewerState(BaseModel):
    liked: bool = False
    saved: bool = False
    following_author: bool = False
    want_to_try: bool = False


class FeedExtras(BaseModel):
    portion_size: Literal["small", "medium", "large"] | None = None
    would_order_again: bool | None = None
    pros: list[str] = []
    cons: list[str] = []
    tags: list[str] = []
    date_tasted: date | None = None
    time_tasted: time | None = None
    visited_with: str | None = None
    is_anonymous: bool | None = None
    price_tier: Literal["$", "$$", "$$$"] | None = None
    presentation: Literal[1, 2, 3] | None = None
    value_prop: Literal[1, 2, 3] | None = None
    execution: Literal[1, 2, 3] | None = None


class FeedItem(BaseModel):
    id: uuid.UUID
    created_at: datetime
    author: FeedAuthor
    dish: FeedDish
    score: float  # 1.0..5.0 en steps de 0.5
    text: str
    media: list[FeedMediaImage] = []
    stats: FeedStats
    viewer_state: FeedViewerState
    extras: FeedExtras | None = None
    # True cuando la review tiene los 3 pilares técnicos completos
    # (presentation + value_prop + execution NOT NULL). El frontend lo usa
    # para resaltarla con sello "Verificada por experto".
    verified_by_expert: bool = False
    # Posición del autor entre los primeros reseñadores del plato (1, 2 o 3).
    # None para el resto. Rankea por created_at ASC, id ASC para desempatar.
    discovery_rank: Literal[1, 2, 3] | None = None


class FeedPage(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None = None


class DishSocialDetail(BaseModel):
    """Dish detail in the social shape consumed by /dishes/[id] page."""

    id: uuid.UUID
    name: str
    restaurant_id: uuid.UUID
    restaurant_name: str
    restaurant_slug: str
    category: str | None = None
    hero_image: str | None = None
    average_score: float
    review_count: int
    would_order_again_pct: float | None = None
    price_range: str | None = None
