import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import hash_password, require_role
from app.models.category import Category
from app.models.dish import Dish, DishReview
from app.models.owner_content import (
    DishReviewOwnerResponse,
    RestaurantOfficialPhoto,
)
from app.models.restaurant import ReservationClick, Restaurant
from app.models.restaurant_claim import ClaimStatus, RestaurantClaim
from app.models.user import User, UserRole
from app.schemas.claim import (
    ClaimAdminListResponse,
    ClaimAdminResponse,
    ClaimApproveBody,
    ClaimClaimantSummary,
    ClaimRejectBody,
    ClaimRestaurantSummary,
    ClaimRevokeBody,
)
from app.services.claim_service import (
    approve_claim,
    reject_claim,
    revoke_claim,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])

SEED_CATEGORIES = [
    {"slug": "dulces", "name": "Dulces", "display_order": 1},
    {"slug": "brunchs", "name": "Brunchs", "display_order": 2},
    {"slug": "desayunos", "name": "Desayunos", "display_order": 3},
    {"slug": "mexico-food", "name": "Mexicana", "display_order": 4},
    {"slug": "japan-food", "name": "Japonesa", "display_order": 5},
    {"slug": "arabic-food", "name": "Árabe", "display_order": 6},
    {"slug": "israelfood", "name": "Israelí", "display_order": 7},
    {"slug": "thaifood", "name": "Tailandesa", "display_order": 8},
    {"slug": "koreanfood", "name": "Coreana", "display_order": 9},
    {"slug": "chinafood", "name": "China", "display_order": 10},
    {"slug": "parrillas", "name": "Parrilla", "display_order": 11},
    {"slug": "brazilfood", "name": "Brasileña", "display_order": 12},
    {"slug": "burguers", "name": "Hamburguesas", "display_order": 13},
    {"slug": "helados", "name": "Helados", "display_order": 14},
    {"slug": "peru-food", "name": "Peruana", "display_order": 15},
]


@router.get("/stats", response_model=dict)
async def get_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> dict:
    restaurants_count = (
        await db.execute(select(func.count()).select_from(Restaurant))
    ).scalar_one()
    dishes_count = (
        await db.execute(select(func.count()).select_from(Dish))
    ).scalar_one()
    reviews_count = (
        await db.execute(select(func.count()).select_from(DishReview))
    ).scalar_one()
    users_count = (
        await db.execute(select(func.count()).select_from(User))
    ).scalar_one()
    categories_count = (
        await db.execute(select(func.count()).select_from(Category))
    ).scalar_one()

    return {
        "restaurants": restaurants_count,
        "dishes": dishes_count,
        "reviews": reviews_count,
        "users": users_count,
        "categories": categories_count,
    }


@router.post("/seed", status_code=status.HTTP_201_CREATED)
async def seed_data(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> dict:
    # Check if categories already exist (one-time use guard)
    existing = await db.execute(select(func.count()).select_from(Category))
    if existing.scalar_one() > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seed data has already been applied. Categories already exist.",
        )

    # Create categories
    created_categories = 0
    for cat_data in SEED_CATEGORIES:
        category = Category(**cat_data)
        db.add(category)
        created_categories += 1

    await db.flush()

    return {
        "message": "Seed data applied successfully",
        "categories_created": created_categories,
    }


# ── Claim review queue ───────────────────────────────────────────────────────


def _hydrate_claim(claim: RestaurantClaim) -> ClaimAdminResponse:
    return ClaimAdminResponse(
        id=claim.id,
        status=ClaimStatus(claim.status),
        verification_method=claim.verification_method,
        contact_email=claim.contact_email,
        evidence_urls=claim.evidence_urls,
        submitted_at=claim.submitted_at,
        reviewed_at=claim.reviewed_at,
        rejection_reason=claim.rejection_reason,
        expires_at=claim.expires_at,
        restaurant=ClaimRestaurantSummary(
            id=claim.restaurant.id,
            slug=claim.restaurant.slug,
            name=claim.restaurant.name,
            location_name=claim.restaurant.location_name,
            is_claimed=claim.restaurant.claimed_by_user_id is not None,
        ),
        claimant=ClaimClaimantSummary.model_validate(claim.claimant),
    )


async def _get_claim_or_404(
    db: AsyncSession, claim_id: uuid.UUID
) -> RestaurantClaim:
    rows = await db.execute(
        select(RestaurantClaim)
        .options(
            selectinload(RestaurantClaim.restaurant),
            selectinload(RestaurantClaim.claimant),
        )
        .where(RestaurantClaim.id == claim_id)
    )
    claim = rows.scalar_one_or_none()
    if claim is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found"
        )
    return claim


@router.get("/claims", response_model=ClaimAdminListResponse)
async def list_admin_claims(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
    status_filter: ClaimStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    stmt = select(RestaurantClaim).options(
        selectinload(RestaurantClaim.restaurant),
        selectinload(RestaurantClaim.claimant),
    )
    if status_filter is not None:
        stmt = stmt.where(RestaurantClaim.status == status_filter.value)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    stmt = (
        stmt.order_by(RestaurantClaim.submitted_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()

    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return {
        "items": [_hydrate_claim(c) for c in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.post("/claims/{claim_id}/approve", response_model=ClaimAdminResponse)
async def admin_approve_claim(
    claim_id: uuid.UUID,
    payload: ClaimApproveBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> ClaimAdminResponse:
    claim = await _get_claim_or_404(db, claim_id)
    await approve_claim(
        db,
        claim,
        reviewer_admin_id=current_user.id,
        notes=payload.notes,
    )
    await db.refresh(claim, attribute_names=["restaurant"])
    return _hydrate_claim(claim)


@router.post("/claims/{claim_id}/reject", response_model=ClaimAdminResponse)
async def admin_reject_claim(
    claim_id: uuid.UUID,
    payload: ClaimRejectBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> ClaimAdminResponse:
    claim = await _get_claim_or_404(db, claim_id)
    await reject_claim(
        db,
        claim,
        reviewer_admin_id=current_user.id,
        reason=payload.reason,
    )
    return _hydrate_claim(claim)


@router.post("/claims/{claim_id}/revoke", response_model=ClaimAdminResponse)
async def admin_revoke_claim(
    claim_id: uuid.UUID,
    payload: ClaimRevokeBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> ClaimAdminResponse:
    claim = await _get_claim_or_404(db, claim_id)
    await revoke_claim(
        db,
        claim,
        reviewer_admin_id=current_user.id,
        reason=payload.reason,
    )
    await db.refresh(claim, attribute_names=["restaurant"])
    return _hydrate_claim(claim)


# ── B2B metrics ──────────────────────────────────────────────────────────────


@router.get("/metrics/b2b", response_model=dict)
async def get_b2b_metrics(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> dict:
    """Vista agregada del estado del pilar B2B.

    Pensado para una página /admin/metrics minimal — los numbers crudos sin
    gráficos. Cuando crezca, mover a un servicio dedicado o un dashboard
    externo (Grafana, Metabase) que lea directo de la DB.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # ----- Reservas afiliadas -----
    restaurants_with_url = (
        await db.execute(
            select(func.count())
            .select_from(Restaurant)
            .where(Restaurant.reservation_url.is_not(None))
        )
    ).scalar_one()

    clicks_total = (
        await db.execute(select(func.count()).select_from(ReservationClick))
    ).scalar_one()
    clicks_7d = (
        await db.execute(
            select(func.count())
            .select_from(ReservationClick)
            .where(ReservationClick.clicked_at >= week_ago)
        )
    ).scalar_one()
    clicks_30d = (
        await db.execute(
            select(func.count())
            .select_from(ReservationClick)
            .where(ReservationClick.clicked_at >= month_ago)
        )
    ).scalar_one()

    top_clicked_rows = (
        await db.execute(
            select(
                Restaurant.slug,
                Restaurant.name,
                func.count(ReservationClick.id).label("clicks"),
            )
            .join(ReservationClick, ReservationClick.restaurant_id == Restaurant.id)
            .group_by(Restaurant.slug, Restaurant.name)
            .order_by(desc("clicks"))
            .limit(5)
        )
    ).all()

    # ----- Claim flow -----
    claims_by_status_rows = (
        await db.execute(
            select(RestaurantClaim.status, func.count())
            .group_by(RestaurantClaim.status)
        )
    ).all()
    claims_by_status = {status: count for status, count in claims_by_status_rows}

    restaurants_total = (
        await db.execute(select(func.count()).select_from(Restaurant))
    ).scalar_one()
    restaurants_claimed = (
        await db.execute(
            select(func.count())
            .select_from(Restaurant)
            .where(Restaurant.claimed_by_user_id.is_not(None))
        )
    ).scalar_one()
    claim_coverage_pct = (
        round(restaurants_claimed * 100 / restaurants_total, 2)
        if restaurants_total
        else 0
    )

    # ----- Owner engagement -----
    reviews_total = (
        await db.execute(select(func.count()).select_from(DishReview))
    ).scalar_one()
    reviews_with_response = (
        await db.execute(
            select(func.count()).select_from(DishReviewOwnerResponse)
        )
    ).scalar_one()
    response_coverage_pct = (
        round(reviews_with_response * 100 / reviews_total, 2)
        if reviews_total
        else 0
    )
    official_photos_total = (
        await db.execute(
            select(func.count()).select_from(RestaurantOfficialPhoto)
        )
    ).scalar_one()
    restaurants_with_photos = (
        await db.execute(
            select(func.count(func.distinct(RestaurantOfficialPhoto.restaurant_id)))
        )
    ).scalar_one()

    return {
        "reservations": {
            "restaurants_with_url": restaurants_with_url,
            "clicks_total": clicks_total,
            "clicks_last_7d": clicks_7d,
            "clicks_last_30d": clicks_30d,
            "top_clicked": [
                {"slug": slug, "name": name, "clicks": clicks}
                for slug, name, clicks in top_clicked_rows
            ],
        },
        "claims": {
            "by_status": claims_by_status,
            "restaurants_total": restaurants_total,
            "restaurants_claimed": restaurants_claimed,
            "coverage_pct": claim_coverage_pct,
        },
        "owner_engagement": {
            "reviews_total": reviews_total,
            "reviews_with_response": reviews_with_response,
            "response_coverage_pct": response_coverage_pct,
            "official_photos_total": official_photos_total,
            "restaurants_with_photos": restaurants_with_photos,
        },
    }
