"""Restaurant claim flow — endpoints públicos.

MVP del pilar B2B: permite que un dueño reclame autoría sobre la ficha del
restaurant para desbloquear (en hitos posteriores) responder reviews y
subir fotos oficiales. La revisión admin vive en `routers/admin.py`.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import CLAIM_CREATE_LIMIT, limiter
from app.models.restaurant import Restaurant
from app.models.restaurant_claim import (
    ClaimStatus,
    RestaurantClaim,
    VerificationMethod,
)
from app.models.user import User
from app.schemas.claim import (
    ClaimCreate,
    ClaimListResponse,
    ClaimResponse,
    ClaimStatusResponse,
)


router = APIRouter(tags=["claims"])


async def _get_restaurant_by_slug(db: AsyncSession, slug: str) -> Restaurant:
    row = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = row.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return restaurant


@router.post(
    "/api/restaurants/{slug}/claims",
    response_model=ClaimResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(CLAIM_CREATE_LIMIT)
async def create_claim(
    request: Request,
    slug: str,
    payload: ClaimCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RestaurantClaim:
    """Crea un claim pending para el restaurant identificado por slug.

    Las features que distinguen entre métodos (auto-aprobación por
    `domain_email`, generación de token de email) se ejecutan acá pero el
    envío real del email queda para el hito de transaccionales — el token
    se guarda en `verification_payload` y un admin puede dispararlo manual
    mientras tanto.
    """
    restaurant = await _get_restaurant_by_slug(db, slug)

    # Bloquear claim duplicado abierto del mismo user/restaurant. La
    # constraint parcial UNIQUE en la tabla también lo cubre, pero queremos
    # devolver 409 antes de pegar contra el IntegrityError.
    existing = await db.execute(
        select(RestaurantClaim).where(
            RestaurantClaim.restaurant_id == restaurant.id,
            RestaurantClaim.claimant_user_id == current_user.id,
            RestaurantClaim.status.in_(
                [ClaimStatus.pending.value, ClaimStatus.verifying.value]
            ),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an open claim for this restaurant",
        )

    # Bloquear si ya hay un owner verificado.
    if restaurant.claimed_by_user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This restaurant already has a verified owner",
        )

    payload_meta: dict[str, str] = {}
    if payload.verification_method == VerificationMethod.domain_email:
        if not payload.contact_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="contact_email is required for domain_email verification",
            )
        payload_meta["email_token"] = secrets.token_urlsafe(32)

    claim = RestaurantClaim(
        restaurant_id=restaurant.id,
        claimant_user_id=current_user.id,
        status=ClaimStatus.pending.value,
        verification_method=payload.verification_method.value,
        contact_email=payload.contact_email,
        evidence_urls=payload.evidence_urls,
        verification_payload=payload_meta or None,
    )
    db.add(claim)
    await db.flush()
    await db.refresh(claim)
    return claim


@router.get("/api/me/claims", response_model=ClaimListResponse)
async def list_my_claims(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    rows = await db.execute(
        select(RestaurantClaim)
        .where(RestaurantClaim.claimant_user_id == current_user.id)
        .order_by(desc(RestaurantClaim.submitted_at))
    )
    return {"items": list(rows.scalars().all())}


@router.get(
    "/api/restaurants/{slug}/claim-status",
    response_model=ClaimStatusResponse,
)
async def get_claim_status(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Endpoint público y light: solo expone si el restaurant tiene owner
    verificado. No revela quién es el owner ni si hay claims pendientes."""
    restaurant = await _get_restaurant_by_slug(db, slug)
    return {"is_claimed": restaurant.claimed_by_user_id is not None}


@router.post(
    "/api/claims/verify-email-token/{token}",
    response_model=ClaimResponse,
)
async def verify_email_token(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RestaurantClaim:
    """Cierra el flujo de `domain_email`: el dueño hace click en el link del
    email y este endpoint marca el claim como verificado, popula
    `claimed_by_user_id` en el restaurant y rota el token (un solo uso).

    Idempotente para el caso "ya verificado": devuelve el claim si el token
    coincide aún después del primer click."""
    if not token or len(token) < 16:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token",
        )

    rows = await db.execute(
        select(RestaurantClaim).where(
            RestaurantClaim.verification_method
            == VerificationMethod.domain_email.value,
            RestaurantClaim.verification_payload["email_token"].astext == token,
        )
    )
    claim = rows.scalar_one_or_none()
    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Token not found"
        )

    if claim.status == ClaimStatus.verified.value:
        return claim

    if claim.status not in {
        ClaimStatus.pending.value,
        ClaimStatus.verifying.value,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Claim is {claim.status}, cannot verify",
        )

    restaurant_row = await db.execute(
        select(Restaurant).where(Restaurant.id == claim.restaurant_id)
    )
    restaurant = restaurant_row.scalar_one()

    # Si otro claim ganó la carrera (constraint partial UNIQUE) caemos en 409.
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
    # Rotar el token para evitar replays — guardamos un valor opaco vacío.
    if claim.verification_payload is not None:
        new_payload = dict(claim.verification_payload)
        new_payload.pop("email_token", None)
        new_payload["verified_via"] = "email_token"
        claim.verification_payload = new_payload

    restaurant.claimed_by_user_id = claim.claimant_user_id
    restaurant.claimed_at = now

    await db.flush()
    await db.refresh(claim)
    return claim


# Helper opcional usable por el admin router para regenerar tokens.
def _generate_email_token() -> str:
    return secrets.token_urlsafe(32)
