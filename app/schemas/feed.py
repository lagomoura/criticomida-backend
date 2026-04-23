"""Feed and review-detail schemas consumed by the social UI."""

import uuid
from datetime import date, datetime
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


class FeedExtras(BaseModel):
    portion_size: Literal["small", "medium", "large"] | None = None
    would_order_again: bool | None = None
    pros: list[str] = []
    cons: list[str] = []
    tags: list[str] = []
    date_tasted: date | None = None
    visited_with: str | None = None
    is_anonymous: bool | None = None
    price_tier: Literal["$", "$$", "$$$"] | None = None


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


class FeedPage(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None = None
