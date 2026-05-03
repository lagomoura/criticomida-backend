"""Endpoints habilitados al verified owner del restaurant.

Encapsula las dos features del Hito 6 — respuestas a reviews y fotos
oficiales del local — bajo un router único. La autorización pasa siempre
por `assert_verified_owner` para que los chequeos no se bifurquen entre
features."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.dish import Dish, DishReview, SentimentLabel
from app.models.owner_content import (
    DishReviewOwnerResponse,
    RestaurantOfficialPhoto,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.owner_content import (
    OfficialPhotoCreate,
    OfficialPhotoRead,
    OfficialPhotosListResponse,
    OwnerResponseRead,
    OwnerResponseUpsert,
    OwnerReviewItem,
    OwnerReviewsListResponse,
)
from app.services.claim_service import assert_verified_owner


router = APIRouter(tags=["owner-content"])


OFFICIAL_PHOTOS_CAP = 5


async def _get_review_with_restaurant(
    db: AsyncSession, review_id: uuid.UUID
) -> tuple[DishReview, uuid.UUID]:
    row = await db.execute(
        select(DishReview, Dish.restaurant_id)
        .join(Dish, DishReview.dish_id == Dish.id)
        .where(DishReview.id == review_id)
    )
    res = row.first()
    if res is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Review not found"
        )
    review, restaurant_id = res
    return review, restaurant_id


async def _get_restaurant_or_404(db: AsyncSession, slug: str) -> Restaurant:
    row = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = row.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return restaurant


# ── Owner response a una review ──────────────────────────────────────────────


@router.get(
    "/api/dish-reviews/{review_id}/owner-response",
    response_model=OwnerResponseRead | None,
)
async def get_owner_response(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DishReviewOwnerResponse | None:
    """Público — cualquiera puede leer la respuesta del restaurante. Devuelve
    None si todavía no hay respuesta registrada."""
    row = await db.execute(
        select(DishReviewOwnerResponse).where(
            DishReviewOwnerResponse.review_id == review_id
        )
    )
    return row.scalar_one_or_none()


@router.put(
    "/api/dish-reviews/{review_id}/owner-response",
    response_model=OwnerResponseRead,
)
async def upsert_owner_response(
    review_id: uuid.UUID,
    payload: OwnerResponseUpsert,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> DishReviewOwnerResponse:
    """Crea o actualiza la respuesta. Idempotente: una sola respuesta por
    review (la PK es review_id)."""
    _, restaurant_id = await _get_review_with_restaurant(db, review_id)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant_id
    )

    existing = (
        await db.execute(
            select(DishReviewOwnerResponse).where(
                DishReviewOwnerResponse.review_id == review_id
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        response = DishReviewOwnerResponse(
            review_id=review_id,
            owner_user_id=current_user.id,
            body=payload.body.strip(),
        )
        db.add(response)
    else:
        existing.body = payload.body.strip()
        existing.owner_user_id = current_user.id
        response = existing

    await db.flush()
    await db.refresh(response)
    return response


@router.delete(
    "/api/dish-reviews/{review_id}/owner-response",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_owner_response(
    review_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    _, restaurant_id = await _get_review_with_restaurant(db, review_id)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant_id
    )

    existing = (
        await db.execute(
            select(DishReviewOwnerResponse).where(
                DishReviewOwnerResponse.review_id == review_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No owner response found",
        )
    await db.delete(existing)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Fotos oficiales del local ───────────────────────────────────────────────


@router.get(
    "/api/restaurants/{slug}/official-photos",
    response_model=OfficialPhotosListResponse,
)
async def list_official_photos(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Público — el detail page renderiza estas fotos con prioridad sobre
    google_photos."""
    restaurant = await _get_restaurant_or_404(db, slug)
    rows = await db.execute(
        select(RestaurantOfficialPhoto)
        .where(RestaurantOfficialPhoto.restaurant_id == restaurant.id)
        .order_by(
            RestaurantOfficialPhoto.display_order,
            desc(RestaurantOfficialPhoto.created_at),
        )
    )
    return {"items": list(rows.scalars().all())}


@router.post(
    "/api/restaurants/{slug}/official-photos",
    response_model=OfficialPhotoRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_official_photo(
    slug: str,
    payload: OfficialPhotoCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> RestaurantOfficialPhoto:
    """Asocia al restaurant una URL ya subida (vía /api/images/upload).
    Cap de 5 por restaurant — devuelve 409 si se intenta superar."""
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )

    count = (
        await db.execute(
            select(func.count())
            .select_from(RestaurantOfficialPhoto)
            .where(RestaurantOfficialPhoto.restaurant_id == restaurant.id)
        )
    ).scalar_one()
    if count >= OFFICIAL_PHOTOS_CAP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Máximo {OFFICIAL_PHOTOS_CAP} fotos oficiales por restaurante",
        )

    photo = RestaurantOfficialPhoto(
        restaurant_id=restaurant.id,
        url=payload.url,
        alt_text=payload.alt_text,
        display_order=payload.display_order,
        uploaded_by_user_id=current_user.id,
    )
    db.add(photo)
    await db.flush()
    await db.refresh(photo)
    return photo


@router.get(
    "/api/restaurants/{slug}/owner/reviews",
    response_model=OwnerReviewsListResponse,
)
async def list_owner_reviews(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    sentiment: Annotated[
        SentimentLabel | None,
        Query(
            description=(
                "Filtrá por sentimiento detectado. Reviews todavía no "
                "analizadas no aparecen cuando se aplica el filtro."
            )
        ),
    ] = None,
    sort: Annotated[
        str | None,
        Query(
            description=(
                "Orden alternativo. ``sentiment_asc`` ordena por score "
                "ascendente (lo más negativo primero); por defecto se "
                "ordena por fecha de creación descendente."
            )
        ),
    ] = None,
) -> dict:
    """Lista plana de todas las reseñas del restaurant para el dashboard del
    owner. Incluye has_owner_response para que el frontend resalte cuáles
    siguen sin contestar y sentiment_label/score para priorizar las
    negativas.

    Solo accesible al verified owner."""
    from app.models.dish import Dish, DishReview
    from app.models.user import User as UserModel
    from app.models.owner_content import DishReviewOwnerResponse

    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )

    # Una sola query con LEFT JOIN al owner-response — evita N+1 al render.
    stmt = (
        select(
            DishReview.id,
            DishReview.dish_id,
            Dish.name.label("dish_name"),
            DishReview.rating,
            DishReview.note,
            DishReview.is_anonymous,
            DishReview.date_tasted,
            DishReview.sentiment_label,
            DishReview.sentiment_score,
            UserModel.display_name,
            UserModel.handle,
            DishReviewOwnerResponse.review_id.label("response_review_id"),
        )
        .join(Dish, DishReview.dish_id == Dish.id)
        .join(UserModel, DishReview.user_id == UserModel.id)
        .outerjoin(
            DishReviewOwnerResponse,
            DishReviewOwnerResponse.review_id == DishReview.id,
        )
        .where(Dish.restaurant_id == restaurant.id)
    )

    if sentiment is not None:
        stmt = stmt.where(DishReview.sentiment_label == sentiment)

    if sort == "sentiment_asc":
        # NULLs last — analyzed-negative bubbles to the top, unanalyzed
        # rows fall to the bottom rather than masquerading as the
        # most-negative slot.
        stmt = stmt.order_by(
            asc(DishReview.sentiment_score).nullslast(),
            DishReview.created_at.desc(),
        )
    else:
        stmt = stmt.order_by(DishReview.created_at.desc())

    rows = (await db.execute(stmt)).all()

    items: list[OwnerReviewItem] = []
    pending_count = 0
    for row in rows:
        has_resp = row.response_review_id is not None
        if not has_resp:
            pending_count += 1
        items.append(
            OwnerReviewItem(
                id=row.id,
                dish_id=row.dish_id,
                dish_name=row.dish_name,
                rating=float(row.rating),
                note=row.note,
                user_display_name="Anónimo" if row.is_anonymous else row.display_name,
                user_handle=None if row.is_anonymous else row.handle,
                is_anonymous=row.is_anonymous,
                date_tasted=row.date_tasted.isoformat(),
                has_owner_response=has_resp,
                sentiment_label=row.sentiment_label,
                sentiment_score=(
                    float(row.sentiment_score)
                    if row.sentiment_score is not None
                    else None
                ),
            )
        )
    return {
        "items": items,
        "total": len(items),
        "pending_count": pending_count,
    }


@router.delete(
    "/api/restaurants/{slug}/official-photos/{photo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_official_photo(
    slug: str,
    photo_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> Response:
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )

    photo = (
        await db.execute(
            select(RestaurantOfficialPhoto).where(
                RestaurantOfficialPhoto.id == photo_id,
                RestaurantOfficialPhoto.restaurant_id == restaurant.id,
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found"
        )

    await db.delete(photo)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
