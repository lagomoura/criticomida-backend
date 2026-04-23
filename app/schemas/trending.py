import uuid
from decimal import Decimal

from pydantic import BaseModel


class TrendingCity(BaseModel):
    city: str
    restaurant_count: int


class TrendingCitiesResponse(BaseModel):
    items: list[TrendingCity]


class TrendingDish(BaseModel):
    dish_id: uuid.UUID
    dish_name: str
    restaurant_id: uuid.UUID
    restaurant_name: str
    city: str
    average_score: Decimal
    total_reviews: int
    # Activity within the requested window.
    likes_recent: int
    comments_recent: int
    reviews_recent: int
    # Composite score used for ranking.
    priority: int


class TrendingDishesResponse(BaseModel):
    items: list[TrendingDish]
    city: str
    days: int
