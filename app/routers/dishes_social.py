"""Social-shape endpoints for the /dishes/[id] page (página estrella v2).

Mirrors the restaurant detail aggregation pattern: detail + aggregates +
photos + diary-stats + related + editorial-blurb. Each endpoint stays small
and composable so the Next.js Server Component can fan out via
Promise.allSettled.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_optional, require_role
from app.models.category import Category
from app.models.dish import Dish, DishReview
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.routers.feed import _build_feed_items
from app.schemas.dish_aggregates import (
    DishAggregatesResponse,
    DishDiaryStats,
    DishEditorialBlurb,
    DishPhotosPage,
    DishSocialDetailEnriched,
    RelatedDishesResponse,
)
from app.schemas.feed import FeedPage
from app.services.dish_editorial_enricher import maybe_schedule_blurb_refresh
from app.services.dish_service import (
    compute_dish_aggregates,
    get_dish_diary_stats,
    get_dish_photos,
    get_related_dishes,
    is_signature_dish,
)

router = APIRouter(prefix="/api/social/dishes", tags=["social-dishes"])


@router.get("/{dish_id}", response_model=DishSocialDetailEnriched)
async def get_dish_social(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> dict:
    stmt = (
        select(Dish, Restaurant, Category, User)
        .join(Restaurant, Restaurant.id == Dish.restaurant_id)
        .outerjoin(Category, Category.id == Restaurant.category_id)
        .outerjoin(User, User.id == Dish.created_by)
        .where(Dish.id == dish_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    dish, restaurant, category, creator = row

    woa_stmt = select(
        func.count(case((DishReview.would_order_again.is_not(None), 1))).label("answered"),
        func.count(case((DishReview.would_order_again.is_(True), 1))).label("yes_count"),
    ).where(DishReview.dish_id == dish_id)
    woa = (await db.execute(woa_stmt)).one()
    woa_pct: float | None
    if woa.answered and woa.answered > 0:
        woa_pct = round(100.0 * woa.yes_count / woa.answered, 1)
    else:
        woa_pct = None

    is_signature = await is_signature_dish(db, dish)

    # Lazy editorial blurb generation. Degrades silent when not configured.
    maybe_schedule_blurb_refresh(background_tasks, dish.id)

    return {
        "id": dish.id,
        "name": dish.name,
        "description": dish.description,
        "restaurant_id": restaurant.id,
        "restaurant_name": restaurant.name,
        "restaurant_slug": restaurant.slug,
        "restaurant_location_name": restaurant.location_name,
        "restaurant_cover_url": restaurant.cover_image_url,
        "restaurant_average_rating": restaurant.computed_rating,
        "restaurant_google_rating": restaurant.google_rating,
        "restaurant_latitude": restaurant.latitude,
        "restaurant_longitude": restaurant.longitude,
        "category": category.name if category else None,
        "cuisine_types": restaurant.cuisine_types,
        "hero_image": dish.cover_image_url,
        "average_score": float(dish.computed_rating or 0),
        "review_count": dish.review_count,
        "would_order_again_pct": woa_pct,
        "price_range": dish.price_tier.value if dish.price_tier else None,
        "is_signature": is_signature,
        "editorial_blurb": dish.editorial_blurb,
        "editorial_source": dish.editorial_blurb_source,
        "created_by_display_name": creator.display_name if creator else None,
    }


@router.get("/{dish_id}/reviews", response_model=FeedPage)
async def get_dish_reviews_social(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)] = None,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    exists = (
        await db.execute(select(Dish.id).where(Dish.id == dish_id).limit(1))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )

    items, has_more = await _build_feed_items(
        db,
        viewer,
        base_filters=[DishReview.dish_id == dish_id],
        cursor_dt=None,
        limit=limit,
        with_extras=True,
    )

    next_cursor = None
    if has_more and items:
        next_cursor = items[-1].created_at.isoformat()
    _ = cursor

    return {"items": items, "next_cursor": next_cursor}


@router.get("/{dish_id}/aggregates", response_model=DishAggregatesResponse)
async def get_dish_aggregates(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    exists = (
        await db.execute(select(Dish.id).where(Dish.id == dish_id).limit(1))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    return await compute_dish_aggregates(db, dish_id)


@router.get("/{dish_id}/photos", response_model=DishPhotosPage)
async def get_dish_photos_endpoint(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=24, ge=1, le=60),
) -> dict:
    exists = (
        await db.execute(select(Dish.id).where(Dish.id == dish_id).limit(1))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    return await get_dish_photos(db, dish_id, limit=limit, cursor=cursor)


@router.get("/{dish_id}/diary-stats", response_model=DishDiaryStats)
async def get_dish_diary_stats_endpoint(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    exists = (
        await db.execute(select(Dish.id).where(Dish.id == dish_id).limit(1))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    return await get_dish_diary_stats(db, dish_id)


@router.get("/{dish_id}/related", response_model=RelatedDishesResponse)
async def get_related_dishes_endpoint(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=6, ge=1, le=20),
) -> dict:
    dish = (
        await db.execute(select(Dish).where(Dish.id == dish_id))
    ).scalar_one_or_none()
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    items = await get_related_dishes(db, dish, limit=limit)
    return {"items": items}


@router.get("/{dish_id}/editorial-blurb")
async def get_dish_editorial_blurb(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    dish = (
        await db.execute(select(Dish).where(Dish.id == dish_id))
    ).scalar_one_or_none()
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    if not dish.editorial_blurb:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    payload = DishEditorialBlurb(
        blurb=dish.editorial_blurb,
        source=dish.editorial_blurb_source or "unknown",
        lang=dish.editorial_blurb_lang,
        cached_at=dish.editorial_cached_at,
    )
    return Response(
        content=payload.model_dump_json(),
        media_type="application/json",
    )


@router.post(
    "/{dish_id}/refresh-editorial",
    response_model=DishEditorialBlurb,
)
async def refresh_dish_editorial_blurb(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> DishEditorialBlurb:
    from app.services.dish_editorial_enricher import refresh_dish_blurb

    dish = (
        await db.execute(select(Dish).where(Dish.id == dish_id))
    ).scalar_one_or_none()
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )

    updated = await refresh_dish_blurb(db, dish_id, force=True)
    if not updated or not dish.editorial_blurb:
        # Refresh did not run (e.g. no API key). Surface as 503 so callers know.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Editorial generator unavailable",
        )

    return DishEditorialBlurb(
        blurb=dish.editorial_blurb,
        source=dish.editorial_blurb_source or "unknown",
        lang=dish.editorial_blurb_lang,
        cached_at=dish.editorial_cached_at,
    )
