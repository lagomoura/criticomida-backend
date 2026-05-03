"""Endpoints para las preferencias de notificación del verified owner.

Separado de ``owner_content`` para mantener acotado el blast radius: este
módulo solo lee/escribe la tabla ``owner_notification_preferences``.

Acceso: el owner verificado del restaurante o un admin (para soporte y
testing). Cuando un admin actúa, la fila siempre se persiste en nombre del
``claimed_by_user_id`` real — el admin no es el dueño de la preferencia, solo
la edita. Sin owner verificado el endpoint no puede persistir nada.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.owner_preferences import OwnerNotificationPreference
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.owner_preferences import (
    OwnerNotificationPreferenceRead,
    OwnerNotificationPreferenceUpdate,
)
from app.services.claim_service import assert_verified_owner


router = APIRouter(tags=["owner-preferences"])


async def _get_restaurant_or_404(db: AsyncSession, slug: str) -> Restaurant:
    row = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = row.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return restaurant


def _resolve_target_owner(
    *, restaurant: Restaurant, viewer: User
) -> uuid.UUID | None:
    """Determina el ``user_id`` que persiste/lee la preferencia.

    - Si el viewer es el owner verificado: retorna ``viewer.id``.
    - Si el viewer es admin (sin ser el owner): retorna
      ``restaurant.claimed_by_user_id`` para que el admin actúe en nombre del
      dueño real.
    - Si no hay owner verificado, retorna ``None`` — el caller decide si eso
      es un default-ON read o un 400 en write.
    """
    if restaurant.claimed_by_user_id == viewer.id:
        return viewer.id
    if viewer.role == UserRole.admin:
        return restaurant.claimed_by_user_id
    return None


@router.get(
    "/api/restaurants/{slug}/owner/notification-preferences",
    response_model=OwnerNotificationPreferenceRead,
)
async def get_owner_notification_preferences(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OwnerNotificationPreferenceRead:
    """Devuelve las preferencias del owner verificado del restaurante.

    Acceso: owner verificado o admin. Si todavía no hay fila o el restaurant
    no tiene owner verificado, asume default ON."""
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )
    target_user_id = _resolve_target_owner(
        restaurant=restaurant, viewer=current_user
    )
    if target_user_id is None:
        return OwnerNotificationPreferenceRead(notify_on_review=True)
    pref = await db.get(
        OwnerNotificationPreference, (target_user_id, restaurant.id)
    )
    return OwnerNotificationPreferenceRead(
        notify_on_review=pref.notify_on_review if pref is not None else True
    )


@router.put(
    "/api/restaurants/{slug}/owner/notification-preferences",
    response_model=OwnerNotificationPreferenceRead,
)
async def update_owner_notification_preferences(
    slug: str,
    payload: OwnerNotificationPreferenceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OwnerNotificationPreferenceRead:
    """Upsert idempotente. Cuando un admin actúa, la fila se guarda en nombre
    del ``claimed_by_user_id``. Sin owner verificado devolvemos 400."""
    restaurant = await _get_restaurant_or_404(db, slug)
    await assert_verified_owner(
        db, user=current_user, restaurant_id=restaurant.id
    )
    target_user_id = _resolve_target_owner(
        restaurant=restaurant, viewer=current_user
    )
    if target_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Restaurant has no verified owner; cannot persist preferences"
            ),
        )
    pref = await db.get(
        OwnerNotificationPreference, (target_user_id, restaurant.id)
    )
    if pref is None:
        pref = OwnerNotificationPreference(
            user_id=target_user_id,
            restaurant_id=restaurant.id,
            notify_on_review=payload.notify_on_review,
        )
        db.add(pref)
    else:
        pref.notify_on_review = payload.notify_on_review
    await db.flush()
    return OwnerNotificationPreferenceRead(
        notify_on_review=pref.notify_on_review
    )
