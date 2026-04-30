"""Lógica de transición de estados del claim flow.

Encapsulamos las transiciones acá para que los routers (admin y verify-email)
no dupliquen reglas. Las constraints partial UNIQUE de la DB son la red de
seguridad; este código es la primera línea — devuelve excepciones HTTP claras
antes de pegarle a la base.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.restaurant import Restaurant
from app.models.restaurant_claim import ClaimStatus, RestaurantClaim


logger = logging.getLogger(__name__)


_OPEN_STATUSES = {ClaimStatus.pending.value, ClaimStatus.verifying.value}


async def approve_claim(
    db: AsyncSession,
    claim: RestaurantClaim,
    *,
    reviewer_admin_id: uuid.UUID | None,
    notes: str | None = None,
) -> RestaurantClaim:
    """Marca el claim como verified y popula claimed_by_user_id en el
    restaurant. Idempotente para claims ya verificados por el mismo claimant.
    """
    if claim.status == ClaimStatus.verified.value:
        return claim

    if claim.status not in _OPEN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Claim is {claim.status}, cannot approve",
        )

    restaurant = (
        await db.execute(
            select(Restaurant).where(Restaurant.id == claim.restaurant_id)
        )
    ).scalar_one()

    if (
        restaurant.claimed_by_user_id is not None
        and restaurant.claimed_by_user_id != claim.claimant_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another claim was verified first",
        )

    now = datetime.now(timezone.utc)
    claim.status = ClaimStatus.verified.value
    claim.reviewed_at = now
    claim.reviewed_by_admin_id = reviewer_admin_id
    if notes is not None:
        meta = dict(claim.verification_payload or {})
        meta["admin_notes"] = notes
        claim.verification_payload = meta

    restaurant.claimed_by_user_id = claim.claimant_user_id
    restaurant.claimed_at = now

    await db.flush()
    notify_claimant(claim, event="approved")
    return claim


async def reject_claim(
    db: AsyncSession,
    claim: RestaurantClaim,
    *,
    reviewer_admin_id: uuid.UUID,
    reason: str,
) -> RestaurantClaim:
    if claim.status not in _OPEN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Claim is {claim.status}, cannot reject",
        )

    claim.status = ClaimStatus.rejected.value
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.reviewed_by_admin_id = reviewer_admin_id
    claim.rejection_reason = reason

    await db.flush()
    notify_claimant(claim, event="rejected", reason=reason)
    return claim


async def revoke_claim(
    db: AsyncSession,
    claim: RestaurantClaim,
    *,
    reviewer_admin_id: uuid.UUID,
    reason: str,
) -> RestaurantClaim:
    """Quita la verificación de un claim ya aprobado. Para abusos detectados
    post-approve. Limpia claimed_by_user_id en el restaurant si este claim era
    el dueño activo."""
    if claim.status != ClaimStatus.verified.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Claim is {claim.status}, only verified claims can be revoked",
        )

    restaurant = (
        await db.execute(
            select(Restaurant).where(Restaurant.id == claim.restaurant_id)
        )
    ).scalar_one()

    claim.status = ClaimStatus.revoked.value
    claim.rejection_reason = reason
    claim.reviewed_at = datetime.now(timezone.utc)
    claim.reviewed_by_admin_id = reviewer_admin_id

    if restaurant.claimed_by_user_id == claim.claimant_user_id:
        restaurant.claimed_by_user_id = None
        restaurant.claimed_at = None

    await db.flush()
    notify_claimant(claim, event="revoked", reason=reason)
    return claim


async def assert_verified_owner(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    restaurant_id: uuid.UUID,
) -> None:
    """Levanta 403 si el user no es el verified owner del restaurant.

    Centralizamos el chequeo acá para que cualquier endpoint que desbloquee
    permisos (responder reviews, fotos oficiales, futuro analytics)
    aplique la misma regla."""
    row = await db.execute(
        select(Restaurant.claimed_by_user_id).where(Restaurant.id == restaurant_id)
    )
    owner = row.scalar_one_or_none()
    if owner is None or owner != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el dueño verificado del restaurante puede hacer esta acción",
        )


def notify_claimant(
    claim: RestaurantClaim,
    *,
    event: str,
    reason: str | None = None,
) -> None:
    """Stub de notificación al claimant.

    Cuando se enchufe email transaccional / in-app notification para claims,
    reemplazar el body de esta función. Por ahora deja un log estructurado
    para que el operador pueda escarbarlo desde Railway.
    """
    logger.info(
        "claim.%s claim_id=%s claimant=%s restaurant=%s reason=%r",
        event,
        claim.id,
        claim.claimant_user_id,
        claim.restaurant_id,
        reason,
    )
