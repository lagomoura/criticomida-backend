"""Social-shape endpoints for the /dishes/[id] page (página estrella v2).

Mirrors the restaurant detail aggregation pattern: detail + aggregates +
photos + diary-stats + related + editorial-blurb. Each endpoint stays small
and composable so the Next.js Server Component can fan out via
Promise.allSettled.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_optional, require_role
from app.models.category import Category
from app.models.dish import Dish, DishReview, WantToTryDish
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.routers.feed import _build_feed_items
from app.schemas.dish_aggregates import (
    DishAggregatesResponse,
    DishDiaryStats,
    DishEditorialBlurb,
    DishPhotosPage,
    DishSocialDetailEnriched,
    DishTimelineBucket,
    DishTimelineResponse,
    FirstDiscoverer,
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
    viewer: Annotated[User | None, Depends(get_current_user_optional)] = None,
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

    want_to_try = False
    if viewer is not None:
        wtt_row = await db.execute(
            select(WantToTryDish.dish_id).where(
                WantToTryDish.user_id == viewer.id,
                WantToTryDish.dish_id == dish.id,
            )
        )
        want_to_try = wtt_row.scalar_one_or_none() is not None

    # Cronistas fundadores: los 3 primeros reseñadores DISTINTOS, ordenados
    # por created_at asc; desempate por DishReview.id. Anónimos no aparecen
    # como "fundadores" porque pierde la narrativa ("quién llegó primero").
    # Si un usuario tiene varias reseñas del mismo plato, contamos solo la
    # más temprana (ese fue el momento en que "descubrió" el plato).
    rn_per_user = func.row_number().over(
        partition_by=DishReview.user_id,
        order_by=[DishReview.created_at.asc(), DishReview.id.asc()],
    ).label("rn_per_user")
    earliest_per_user = (
        select(
            DishReview.id.label("review_id"),
            rn_per_user,
        )
        .where(DishReview.dish_id == dish_id, DishReview.is_anonymous.is_(False))
        .subquery()
    )
    first_rows = (
        await db.execute(
            select(DishReview, User)
            .join(earliest_per_user, earliest_per_user.c.review_id == DishReview.id)
            .join(User, User.id == DishReview.user_id)
            .where(earliest_per_user.c.rn_per_user == 1)
            .order_by(DishReview.created_at.asc(), DishReview.id.asc())
            .limit(3)
        )
    ).all()
    first_discoverers = [
        FirstDiscoverer(
            rank=idx + 1,  # type: ignore[arg-type]
            user_id=usr.id,
            handle=usr.handle,
            display_name=usr.display_name,
            avatar_url=usr.avatar_url,
            discovered_at=rev.created_at,
            review_id=rev.id,
        )
        for idx, (rev, usr) in enumerate(first_rows)
    ]

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
        "want_to_try": want_to_try,
        "first_discoverers": first_discoverers,
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


@router.get("/{dish_id}/timeline", response_model=DishTimelineResponse)
async def get_dish_timeline(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    granularity: Literal["quarter", "month"] = Query(default="quarter"),
) -> DishTimelineResponse:
    """Evolución del plato a lo largo del tiempo: rating + 3 pilares por bucket.

    Agrupa por trimestre (default) o mes usando `date_tasted`. Reseñas sin
    `date_tasted` se ignoran (en la práctica son raras: el form lo pide).
    El frontend muestra esto como narrativa del gastronerd ("¿cómo cambió este
    plato a lo largo del tiempo?") — el quick-win clave del anzuelo.
    """
    exists = (
        await db.execute(select(Dish.id).where(Dish.id == dish_id).limit(1))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )

    if granularity == "quarter":
        period_expr = func.concat(
            func.to_char(DishReview.date_tasted, "YYYY"),
            "-Q",
            func.to_char(DishReview.date_tasted, "Q"),
        )
    else:
        period_expr = func.to_char(DishReview.date_tasted, "YYYY-MM")

    rows = (
        await db.execute(
            select(
                period_expr.label("period"),
                func.count(DishReview.id).label("review_count"),
                func.avg(DishReview.rating).label("avg_rating"),
                func.avg(DishReview.presentation).label("presentation_avg"),
                func.avg(DishReview.value_prop).label("value_prop_avg"),
                func.avg(DishReview.execution).label("execution_avg"),
            )
            .where(
                DishReview.dish_id == dish_id,
                DishReview.date_tasted.is_not(None),
            )
            .group_by(period_expr)
            .order_by(period_expr.asc())
        )
    ).all()

    buckets: list[DishTimelineBucket] = []
    prev_avg: float | None = None
    for r in rows:
        avg_f = float(r.avg_rating) if r.avg_rating is not None else 0.0
        delta = (
            Decimal(str(round(avg_f - prev_avg, 2))) if prev_avg is not None else None
        )
        buckets.append(
            DishTimelineBucket(
                period=str(r.period),
                review_count=int(r.review_count or 0),
                avg_rating=Decimal(str(round(avg_f, 2))),
                presentation_avg=(
                    round(float(r.presentation_avg), 2)
                    if r.presentation_avg is not None
                    else None
                ),
                value_prop_avg=(
                    round(float(r.value_prop_avg), 2)
                    if r.value_prop_avg is not None
                    else None
                ),
                execution_avg=(
                    round(float(r.execution_avg), 2)
                    if r.execution_avg is not None
                    else None
                ),
                delta_rating=delta,
            )
        )
        prev_avg = avg_f

    return DishTimelineResponse(granularity=granularity, buckets=buckets)


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
