"""Endpoints del verified owner para gestionar la foto oficial de cada plato.

Separado de ``owner_content`` porque el dominio es distinto (no es contenido
del local sino un atributo del dish), y para no inflar más ese archivo.

La foto oficial del dish vive en ``dishes.cover_image_url`` — campo simple
que ya existía y que el frontend usa como primera opción de la cascada de
fallbacks (cover oficial → última review con foto → fallback genérico).

Patrón de upload (idéntico a ``RestaurantOfficialPhoto``):

1. El cliente sube el binario a ``POST /api/images/upload`` con
   ``entity_type=dish_cover`` y recibe una URL relativa (``/uploads/…``).
2. El cliente llama a ``PUT .../cover`` con esa URL para promoverla a
   foto oficial. Si en su lugar quiere reusar una foto ya subida por un
   comensal en una review, primero la elige vía ``GET .../photo-candidates``
   y pasa la URL recibida.

Autorización: ``assert_verified_owner`` — admins entran por el bypass.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.dish import Dish, DishReview, DishReviewImage
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.owner_content import (
    DishCoverRead,
    DishCoverUpdate,
    DishPhotoCandidate,
    DishPhotoCandidatesResponse,
)
from app.services.claim_service import assert_verified_owner


router = APIRouter(tags=["owner-dishes"])


async def _get_restaurant_or_404(db: AsyncSession, slug: str) -> Restaurant:
    row = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = row.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return restaurant


async def _get_dish_in_restaurant(
    db: AsyncSession, *, dish_id: uuid.UUID, restaurant_id: uuid.UUID
) -> Dish:
    """Devuelve el dish solo si pertenece al restaurant indicado.

    Devuelve 404 también cuando existe en otro restaurant — no exponemos al
    owner que un dish ajeno existe."""
    row = await db.execute(
        select(Dish).where(Dish.id == dish_id, Dish.restaurant_id == restaurant_id)
    )
    dish = row.scalar_one_or_none()
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dish not found"
        )
    return dish


@router.put(
    "/api/restaurants/{slug}/dishes/{dish_id}/cover",
    response_model=DishCoverRead,
)
async def set_dish_cover(
    slug: str,
    dish_id: uuid.UUID,
    payload: DishCoverUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishCoverRead:
    """Promueve una URL ya subida a foto oficial del plato. Idempotente:
    pisa el ``cover_image_url`` actual sin importar quién lo había seteado
    (incluyendo el cron de auto-promote). El owner siempre gana."""
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )
    dish = await _get_dish_in_restaurant(
        db, dish_id=dish_id, restaurant_id=restaurant.id
    )

    dish.cover_image_url = payload.url
    await db.flush()
    return DishCoverRead(dish_id=dish.id, cover_image_url=dish.cover_image_url)


@router.delete(
    "/api/restaurants/{slug}/dishes/{dish_id}/cover",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_dish_cover(
    slug: str,
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    """Vuelve al fallback del frontend (review más reciente con foto). No
    borra fotos de reviews — solo limpia el cover oficial del plato."""
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )
    dish = await _get_dish_in_restaurant(
        db, dish_id=dish_id, restaurant_id=restaurant.id
    )

    dish.cover_image_url = None
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/api/restaurants/{slug}/dishes/{dish_id}/photo-candidates",
    response_model=DishPhotoCandidatesResponse,
)
async def list_photo_candidates(
    slug: str,
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishPhotoCandidatesResponse:
    """Lista las fotos UGC del plato disponibles para promover a oficial.

    Orden: por rating de la review desc, luego fecha desc, luego
    display_order asc. Pensado para alimentar un picker en el dashboard
    del owner — el owner clickea una y la promueve con el PUT.

    Las reviews anónimas se incluyen pero no exponen el autor (consistente
    con cómo se respetan en el resto del owner-dashboard)."""
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )
    dish = await _get_dish_in_restaurant(
        db, dish_id=dish_id, restaurant_id=restaurant.id
    )

    rows = await db.execute(
        select(
            DishReview.id.label("review_id"),
            DishReview.rating,
            DishReview.created_at,
            DishReview.is_anonymous,
            DishReviewImage.id.label("image_id"),
            DishReviewImage.url,
            DishReviewImage.alt_text,
            User.display_name,
        )
        .join(DishReviewImage, DishReviewImage.dish_review_id == DishReview.id)
        .join(User, User.id == DishReview.user_id, isouter=True)
        .where(DishReview.dish_id == dish.id)
        .order_by(
            desc(DishReview.rating),
            desc(DishReview.created_at),
            DishReviewImage.display_order.asc(),
        )
    )

    items: list[DishPhotoCandidate] = []
    for r in rows.all():
        items.append(
            DishPhotoCandidate(
                review_id=r.review_id,
                image_id=r.image_id,
                url=r.url,
                alt_text=r.alt_text,
                review_rating=float(r.rating),
                review_created_at=r.created_at,
                user_display_name=None if r.is_anonymous else r.display_name,
                is_anonymous=r.is_anonymous,
            )
        )
    return DishPhotoCandidatesResponse(items=items)
