"""Unified search across dishes, restaurants, and users.

Word-prefix match (case- and accent-insensitive) over the canonical name
field of each entity, sorted alphabetically. Users match against both
``handle`` and ``display_name`` so a query like "Julián" finds the user
whose handle is ``julianp`` but display name is "Julián Pérez".
"""

import re
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


def _word_prefix(column, q: str):
    # Match q at the start of column or right after whitespace, ignoring
    # case and diacritics on both sides.
    pattern = r"(^|\s)" + re.escape(q)
    column_norm = func.unaccent(func.lower(column))
    pattern_norm = func.unaccent(func.lower(pattern))
    return column_norm.op("~")(pattern_norm)


def _alpha(column):
    return func.unaccent(func.lower(column)).asc()


@router.get("", response_model=SearchResponse)
async def search_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str = Query(min_length=1, max_length=100),
) -> SearchResponse:
    trimmed = q.strip()
    if not trimmed:
        return SearchResponse(dishes=[], restaurants=[], users=[])

    # ── Dishes ────────────────────────────────────────────────────────────
    dish_stmt = (
        select(Dish, Restaurant, Category)
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .outerjoin(Category, Restaurant.category_id == Category.id)
        .where(_word_prefix(Dish.name, trimmed))
        .order_by(_alpha(Dish.name))
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
        .where(_word_prefix(Restaurant.name, trimmed))
        .order_by(_alpha(Restaurant.name))
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
            User.handle.is_not(None),
            or_(
                _word_prefix(User.handle, trimmed),
                _word_prefix(User.display_name, trimmed),
            ),
        )
        .order_by(_alpha(User.display_name))
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
