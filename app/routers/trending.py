"""Trending by city.

- `GET /api/trending/cities` — distinct city values with restaurant counts,
  populates the frontend city picker.
- `GET /api/trending/dishes?city=&days=&limit=` — dishes in restaurants of
  that city ranked by recent engagement.

Scoring formula:
    priority = likes_in_window * 1 + comments_in_window * 2 + reviews_in_window * 3

A dish is only included when it has at least one activity unit in the window —
empty tails are not useful to the user and dilute the UI.
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.dish import Dish, DishReview
from app.models.like import Like
from app.models.restaurant import Restaurant
from app.models.social import Comment
from app.schemas.trending import (
    TrendingCitiesResponse,
    TrendingCity,
    TrendingDish,
    TrendingDishesResponse,
)

router = APIRouter(prefix="/api/trending", tags=["trending"])


@router.get("/cities", response_model=TrendingCitiesResponse)
async def list_trending_cities(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
) -> TrendingCitiesResponse:
    stmt = (
        select(Restaurant.city, func.count().label("c"))
        .where(Restaurant.city.is_not(None))
        .group_by(Restaurant.city)
        .order_by(func.count().desc(), Restaurant.city.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return TrendingCitiesResponse(
        items=[TrendingCity(city=city, restaurant_count=int(count)) for city, count in rows]
    )


@router.get("/dishes", response_model=TrendingDishesResponse)
async def list_trending_dishes(
    db: Annotated[AsyncSession, Depends(get_db)],
    city: str = Query(min_length=1, max_length=100),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=20, ge=1, le=50),
) -> TrendingDishesResponse:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Per-dish activity counts in the window, built as scalar correlated
    # subqueries. PostgreSQL evaluates these once per outer row — with
    # typical dish volumes (~hundreds) that's fine for MVP.
    likes_recent = (
        select(func.count())
        .select_from(Like)
        .join(DishReview, Like.review_id == DishReview.id)
        .where(DishReview.dish_id == Dish.id, Like.created_at >= cutoff)
        .correlate(Dish)
        .scalar_subquery()
    )
    comments_recent = (
        select(func.count())
        .select_from(Comment)
        .join(DishReview, Comment.review_id == DishReview.id)
        .where(
            DishReview.dish_id == Dish.id,
            Comment.created_at >= cutoff,
            Comment.removed_at.is_(None),
        )
        .correlate(Dish)
        .scalar_subquery()
    )
    reviews_recent = (
        select(func.count())
        .select_from(DishReview)
        .where(DishReview.dish_id == Dish.id, DishReview.created_at >= cutoff)
        .correlate(Dish)
        .scalar_subquery()
    )

    priority = likes_recent * 1 + comments_recent * 2 + reviews_recent * 3

    stmt = (
        select(
            Dish,
            Restaurant,
            likes_recent.label("likes_recent"),
            comments_recent.label("comments_recent"),
            reviews_recent.label("reviews_recent"),
            priority.label("priority"),
        )
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .where(Restaurant.city == city)
        .where(priority > 0)
        .order_by(priority.desc(), Dish.computed_rating.desc().nullslast())
        .limit(limit)
    )

    rows = (await db.execute(stmt)).all()
    items = [
        TrendingDish(
            dish_id=dish.id,
            dish_name=dish.name,
            restaurant_id=restaurant.id,
            restaurant_name=restaurant.name,
            city=restaurant.city or "",
            average_score=dish.computed_rating,
            total_reviews=dish.review_count,
            likes_recent=int(lr),
            comments_recent=int(cr),
            reviews_recent=int(rvr),
            priority=int(pr),
        )
        for dish, restaurant, lr, cr, rvr, pr in rows
    ]

    return TrendingDishesResponse(items=items, city=city, days=days)
