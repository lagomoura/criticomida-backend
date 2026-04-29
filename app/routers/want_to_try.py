"""Wishlist endpoints: 'Quiero probarlo' a nivel plato.

Convive (intencionalmente) con el bookmark de reviews (`/api/reviews/{id}/save`).
Aquel guarda una reseña concreta; éste guarda el plato — la intención del
usuario es 'lo voy a pedir', no 'quiero releer esta reseña'.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.dish import Dish, WantToTryDish
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.want_to_try import (
    WantToTryActionResponse,
    WantToTryItem,
    WantToTryPage,
)

router = APIRouter(tags=["want-to-try"])


async def _ensure_dish(db: AsyncSession, dish_id: uuid.UUID) -> None:
    result = await db.execute(select(Dish.id).where(Dish.id == dish_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Plato no encontrado")


@router.post(
    "/api/dishes/{dish_id}/want-to-try", response_model=WantToTryActionResponse
)
async def add_want_to_try(
    dish_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WantToTryActionResponse:
    """Idempotente: marcar como 'quiero probarlo' es siempre seguro."""
    await _ensure_dish(db, dish_id)
    existing = await db.execute(
        select(WantToTryDish).where(
            WantToTryDish.user_id == current_user.id,
            WantToTryDish.dish_id == dish_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(WantToTryDish(user_id=current_user.id, dish_id=dish_id))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()

    return WantToTryActionResponse(dish_id=dish_id, want_to_try=True)


@router.delete(
    "/api/dishes/{dish_id}/want-to-try", response_model=WantToTryActionResponse
)
async def remove_want_to_try(
    dish_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WantToTryActionResponse:
    await _ensure_dish(db, dish_id)
    existing = await db.execute(
        select(WantToTryDish).where(
            WantToTryDish.user_id == current_user.id,
            WantToTryDish.dish_id == dish_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()

    return WantToTryActionResponse(dish_id=dish_id, want_to_try=False)


@router.get("/api/users/me/want-to-try", response_model=WantToTryPage)
async def list_my_want_to_try(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> WantToTryPage:
    """Wishlist del usuario, ordenado por fecha de guardado descendente.

    El cursor es el `created_at` ISO del último item de la página.
    """
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    stmt = (
        select(
            WantToTryDish.created_at.label("saved_at"),
            Dish.id.label("dish_id"),
            Dish.name.label("dish_name"),
            Dish.cover_image_url,
            Dish.computed_rating,
            Dish.review_count,
            Restaurant.id.label("restaurant_id"),
            Restaurant.slug.label("restaurant_slug"),
            Restaurant.name.label("restaurant_name"),
            Restaurant.city.label("restaurant_city"),
            Restaurant.latitude,
            Restaurant.longitude,
        )
        .join(Dish, Dish.id == WantToTryDish.dish_id)
        .join(Restaurant, Restaurant.id == Dish.restaurant_id)
        .where(WantToTryDish.user_id == current_user.id)
        .order_by(WantToTryDish.created_at.desc(), WantToTryDish.dish_id)
        .limit(limit + 1)
    )
    if cursor_dt is not None:
        stmt = stmt.where(WantToTryDish.created_at < cursor_dt)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]

    items = [
        WantToTryItem(
            dish_id=r.dish_id,
            dish_name=r.dish_name,
            cover_image_url=r.cover_image_url,
            computed_rating=r.computed_rating,
            review_count=r.review_count,
            restaurant_id=r.restaurant_id,
            restaurant_slug=r.restaurant_slug,
            restaurant_name=r.restaurant_name,
            restaurant_city=r.restaurant_city,
            restaurant_latitude=r.latitude,
            restaurant_longitude=r.longitude,
            saved_at=r.saved_at,
        )
        for r in trimmed
    ]
    next_cursor = items[-1].saved_at.isoformat() if has_more and items else None
    return WantToTryPage(items=items, next_cursor=next_cursor)
