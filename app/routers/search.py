"""Unified search across dishes, restaurants, and users.

Backed by ILIKE queries — good enough for the dev corpus. When we introduce
pg_trgm or Postgres full-text search this router swaps in with the same
response shape.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.category import Category
from app.models.dish import Dish
from app.models.follow import Follow
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.search import (
    DishSearchResult,
    RestaurantSearchResult,
    SearchResponse,
    UserSearchResult,
)

router = APIRouter(prefix="/api/search", tags=["search"])

_PER_TAB_LIMIT = 20


@router.get("", response_model=SearchResponse)
async def search_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(min_length=1, max_length=100),
) -> SearchResponse:
    trimmed = q.strip()
    if not trimmed:
        return SearchResponse(dishes=[], restaurants=[], users=[])

    pattern = f"%{trimmed}%"

    # ── Dishes ────────────────────────────────────────────────────────────
    dish_stmt = (
        select(Dish, Restaurant, Category)
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .outerjoin(Category, Restaurant.category_id == Category.id)
        .where(
            or_(
                Dish.name.ilike(pattern),
                Restaurant.name.ilike(pattern),
                Category.name.ilike(pattern),
            )
        )
        .order_by(Dish.computed_rating.desc().nullslast(), Dish.review_count.desc())
        .limit(_PER_TAB_LIMIT)
    )
    dish_rows = (await db.execute(dish_stmt)).all()
    dishes = [
        DishSearchResult(
            id=dish.id,
            name=dish.name,
            restaurant_id=restaurant.id,
            restaurant_name=restaurant.name,
            category=category.name if category else None,
            average_score=float(dish.computed_rating or 0),
            review_count=int(dish.review_count or 0),
        )
        for dish, restaurant, category in dish_rows
    ]

    # ── Restaurants ───────────────────────────────────────────────────────
    dish_count_sq = (
        select(Dish.restaurant_id, func.count().label("c"))
        .group_by(Dish.restaurant_id)
        .subquery()
    )
    rest_stmt = (
        select(Restaurant, Category, func.coalesce(dish_count_sq.c.c, 0))
        .outerjoin(Category, Restaurant.category_id == Category.id)
        .outerjoin(dish_count_sq, dish_count_sq.c.restaurant_id == Restaurant.id)
        .where(
            or_(
                Restaurant.name.ilike(pattern),
                Category.name.ilike(pattern),
            )
        )
        .order_by(Restaurant.computed_rating.desc().nullslast())
        .limit(_PER_TAB_LIMIT)
    )
    rest_rows = (await db.execute(rest_stmt)).all()
    restaurants = [
        RestaurantSearchResult(
            id=restaurant.id,
            name=restaurant.name,
            category=category.name if category else None,
            dish_count=int(dish_count or 0),
        )
        for restaurant, category, dish_count in rest_rows
    ]

    # ── Users ─────────────────────────────────────────────────────────────
    followers_sq = (
        select(Follow.following_id, func.count().label("c"))
        .group_by(Follow.following_id)
        .subquery()
    )
    user_stmt = (
        select(User, func.coalesce(followers_sq.c.c, 0))
        .outerjoin(followers_sq, followers_sq.c.following_id == User.id)
        .where(
            or_(
                User.display_name.ilike(pattern),
                User.handle.ilike(pattern),
                User.bio.ilike(pattern),
            )
        )
        .limit(_PER_TAB_LIMIT)
    )
    user_rows = (await db.execute(user_stmt)).all()
    users = [
        UserSearchResult(
            id=user.id,
            display_name=user.display_name,
            handle=user.handle,
            avatar_url=user.avatar_url,
            bio=user.bio,
            followers=int(followers or 0),
        )
        for user, followers in user_rows
    ]

    return SearchResponse(dishes=dishes, restaurants=restaurants, users=users)
