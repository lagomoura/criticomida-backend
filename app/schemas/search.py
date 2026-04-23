import uuid

from pydantic import BaseModel


class DishSearchResult(BaseModel):
    id: uuid.UUID
    name: str
    restaurant_id: uuid.UUID
    restaurant_name: str
    category: str | None = None
    average_score: float
    review_count: int


class RestaurantSearchResult(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None = None
    dish_count: int


class UserSearchResult(BaseModel):
    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    followers: int = 0


class SearchResponse(BaseModel):
    dishes: list[DishSearchResult]
    restaurants: list[RestaurantSearchResult]
    users: list[UserSearchResult]
