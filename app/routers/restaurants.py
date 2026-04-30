import re
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.db_errors import is_unique_violation
from app.middleware.auth import get_current_user, get_current_user_optional, require_role
from app.models.category import Category
from app.models.restaurant import ReservationClick, Restaurant
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse
from app.schemas.discovery import MapBboxResponse
from app.schemas.restaurant import (
    DiaryStatsResponse,
    MatchCandidatesResponse,
    NearbyRestaurantsResponse,
    ReservationClickCreate,
    RestaurantAggregatesResponse,
    RestaurantCreate,
    RestaurantCreateResponse,
    RestaurantListResponse,
    RestaurantMergeRequest,
    RestaurantMergeResponse,
    RestaurantPhotosResponse,
    RestaurantResponse,
    RestaurantUpdate,
    SignatureDishesResponse,
)
from app.services.discovery_service import discover_restaurants_in_bbox
from app.services.google_places_enricher import (
    cache_is_fresh,
    refresh_restaurant_from_google,
)
from app.services.image_cleanup import delete_images_for_restaurant
from app.services.restaurant_service import (
    find_match_candidates,
    find_restaurant_by_place_id,
    find_restaurant_by_redirect,
    get_nearby_restaurants,
    get_restaurant_aggregates,
    get_restaurant_by_slug,
    get_restaurant_detail,
    get_restaurant_diary_stats,
    get_restaurant_list,
    get_restaurant_photos,
    get_signature_dishes,
    merge_restaurants,
)

router = APIRouter(prefix="/api/restaurants", tags=["restaurants"])


def _slugify(name: str) -> str:
    """Generate a URL-friendly slug from a name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


@router.get("", response_model=PaginatedResponse[RestaurantListResponse])
async def list_restaurants(
    db: Annotated[AsyncSession, Depends(get_db)],
    category_slug: str | None = None,
    search: str | None = None,
    min_rating: Decimal | None = None,
    max_rating: Decimal | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
) -> dict:
    restaurants, total = await get_restaurant_list(
        db,
        category_slug=category_slug,
        search=search,
        min_rating=min_rating,
        max_rating=max_rating,
        page=page,
        per_page=per_page,
    )
    total_pages = (total + per_page - 1) // per_page if total > 0 else 0
    return {
        "items": restaurants,
        "total": total,
        "page": page,
        "page_size": per_page,
        "total_pages": total_pages,
    }


@router.get("/match-candidates", response_model=MatchCandidatesResponse)
async def match_candidates_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    name: str = Query(..., min_length=1, max_length=200),
    lat: float = Query(..., ge=-90.0, le=90.0),
    lng: float = Query(..., ge=-180.0, le=180.0),
    exclude_place_id: str | None = Query(None, max_length=200),
) -> dict:
    """Surface potential duplicates of a restaurant the user is about to add.

    Used by the AddRestaurantModal flow to ask "did you mean X?" before
    committing — covers the case where Google returns two distinct place_ids
    for the same physical venue (Fase 2.2). The Fase 2.1 dedup by
    `google_place_id` handles the same-place_id case separately.
    """
    items = await find_match_candidates(
        db,
        name=name,
        latitude=lat,
        longitude=lng,
        exclude_place_id=exclude_place_id,
    )
    return {"items": items}


@router.get("/in-bbox", response_model=MapBboxResponse)
async def restaurants_in_bbox(
    db: Annotated[AsyncSession, Depends(get_db)],
    min_lat: float = Query(..., ge=-90.0, le=90.0),
    min_lng: float = Query(..., ge=-180.0, le=180.0),
    max_lat: float = Query(..., ge=-90.0, le=90.0),
    max_lng: float = Query(..., ge=-180.0, le=180.0),
    limit: int = Query(default=200, ge=1, le=500),
    sort: str = Query(default="geek_score"),
    include_empty: bool = Query(default=False),
    chef_only: bool = Query(default=False),
) -> MapBboxResponse:
    """Restaurantes dentro del bbox + sus platos destacados.

    `sort`: `geek_score` (default) | `value_prop` | `trending`.
    `include_empty=true` agrega también locales sin reviews (pines grises
    para CTAs "sé el primero en reseñar").
    `chef_only=true` filtra a solo restaurantes con Chef Badge (al menos un
    plato con execution_avg ≥ 2.7 y suficientes reviews). Cuando se combina
    con `include_empty`, los locales sin reviews quedan excluidos.
    """
    if min_lat > max_lat or min_lng > max_lng:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_lat must be <= max_lat and min_lng must be <= max_lng",
        )
    if sort not in ("geek_score", "value_prop", "trending"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sort must be one of: geek_score, value_prop, trending",
        )
    return await discover_restaurants_in_bbox(
        db,
        min_lat=min_lat,
        min_lng=min_lng,
        max_lat=max_lat,
        max_lng=max_lng,
        limit=limit,
        sort=sort,
        include_empty=include_empty,
        chef_only=chef_only,
    )


@router.get("/{slug}", response_model=RestaurantResponse)
async def get_restaurant(
    slug: str,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Restaurant:
    restaurant = await get_restaurant_detail(db, slug)
    if restaurant is None:
        # Slug may belong to a restaurant that was merged into another. Look up
        # the redirect table and serve the merge target so old links keep working.
        redirected_id = await find_restaurant_by_redirect(db, slug)
        if redirected_id is not None:
            restaurant = await get_restaurant_detail(db, str(redirected_id))
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )
    # Lazy enrichment: schedule a Google Places refresh when cache is stale
    # and a place_id is available. The current response is served from
    # what's already in the DB (or nothing) — the next page load will see
    # the new fields once the background task commits.
    if restaurant.google_place_id and not cache_is_fresh(
        restaurant.google_cached_at
    ):
        background_tasks.add_task(
            _refresh_in_background, restaurant.id
        )
    return restaurant


async def _refresh_in_background(restaurant_id) -> None:
    """Open a fresh DB session inside the background task — request-scoped
    sessions are closed by the time BackgroundTasks runs."""
    from app.database import async_session

    async with async_session() as session:
        await refresh_restaurant_from_google(session, restaurant_id, force=False)


@router.post("", response_model=RestaurantCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_restaurant(
    restaurant_data: RestaurantCreate,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> RestaurantCreateResponse:
    # Verify category exists only when one is provided
    if restaurant_data.category_id is not None:
        cat_result = await db.execute(
            select(Category).where(Category.id == restaurant_data.category_id)
        )
        if cat_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Category not found",
            )

    place_id = restaurant_data.google_place_id

    # Pre-INSERT dedup: if this Google Place is already in the DB, return it.
    if place_id:
        existing = await find_restaurant_by_place_id(db, place_id, eager=True)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return _build_create_response(existing, existed=True)

    # Cache the creator id before the loop — a rollback inside the retry path
    # expires `current_user`'s attributes, and re-accessing `.id` would trigger
    # a SELECT outside SQLAlchemy's async greenlet context (MissingGreenlet).
    creator_id = current_user.id
    payload = restaurant_data.model_dump(exclude={"slug"})
    base_slug = (restaurant_data.slug or "").strip() or _slugify(
        restaurant_data.name
    )
    max_attempts = 8

    for attempt in range(max_attempts):
        slug = base_slug if attempt == 0 else f"{base_slug}-{uuid.uuid4().hex[:8]}"

        candidate = Restaurant(
            **payload,
            slug=slug,
            created_by=creator_id,
        )
        db.add(candidate)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if not is_unique_violation(exc):
                raise
            # Race: another concurrent request may have just inserted the same
            # google_place_id. Re-check before assuming it was a slug collision.
            if place_id:
                existing = await find_restaurant_by_place_id(db, place_id, eager=True)
                if existing is not None:
                    response.status_code = status.HTTP_200_OK
                    return _build_create_response(existing, existed=True)
            if attempt == max_attempts - 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Could not allocate a unique restaurant slug",
                ) from exc
            continue

        await db.refresh(candidate)
        result = await db.execute(
            select(Restaurant)
            .options(
                selectinload(Restaurant.category),
                selectinload(Restaurant.creator),
            )
            .where(Restaurant.id == candidate.id)
        )
        return _build_create_response(result.scalar_one(), existed=False)

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Could not allocate a unique restaurant slug",
    )


def _build_create_response(
    restaurant: Restaurant, *, existed: bool
) -> RestaurantCreateResponse:
    payload = RestaurantCreateResponse.model_validate(restaurant, from_attributes=True)
    payload.existed = existed
    return payload


@router.put("/{slug}", response_model=RestaurantResponse)
async def update_restaurant(
    slug: str,
    restaurant_data: RestaurantUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> Restaurant:
    result = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    update_data = restaurant_data.model_dump(exclude_unset=True)

    # If updating category_id, verify it exists
    if "category_id" in update_data:
        cat_result = await db.execute(
            select(Category).where(Category.id == update_data["category_id"])
        )
        if cat_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Category not found",
            )

    for field, value in update_data.items():
        setattr(restaurant, field, value)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if is_unique_violation(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Restaurant slug already in use",
            ) from exc
        raise

    # Reload with relationships
    reload_result = await db.execute(
        select(Restaurant)
        .options(
            selectinload(Restaurant.category),
            selectinload(Restaurant.creator),
        )
        .where(Restaurant.id == restaurant.id)
    )
    return reload_result.scalar_one()


@router.get("/{slug}/aggregates", response_model=RestaurantAggregatesResponse)
async def get_restaurant_aggregates_endpoint(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    restaurant = await get_restaurant_by_slug(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return await get_restaurant_aggregates(db, restaurant.id)


@router.get("/{slug}/photos", response_model=RestaurantPhotosResponse)
async def get_restaurant_photos_endpoint(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=24, ge=1, le=60),
    cursor: str | None = None,
) -> dict:
    restaurant = await get_restaurant_by_slug(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return await get_restaurant_photos(db, restaurant.id, limit=limit, cursor=cursor)


@router.get("/{slug}/diary-stats", response_model=DiaryStatsResponse)
async def get_restaurant_diary_stats_endpoint(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    restaurant = await get_restaurant_by_slug(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return await get_restaurant_diary_stats(db, restaurant.id)


@router.get("/{slug}/signature-dishes", response_model=SignatureDishesResponse)
async def get_signature_dishes_endpoint(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=4, ge=1, le=12),
) -> dict:
    restaurant = await get_restaurant_by_slug(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    items = await get_signature_dishes(db, restaurant.id, limit=limit)
    return {"items": items}


@router.get("/{slug}/nearby", response_model=NearbyRestaurantsResponse)
async def get_nearby_restaurants_endpoint(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    radius_km: float = Query(default=3.0, ge=0.1, le=20.0),
    limit: int = Query(default=6, ge=1, le=20),
) -> dict:
    restaurant = await get_restaurant_by_slug(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    if restaurant.latitude is None or restaurant.longitude is None:
        return {"items": []}

    items = await get_nearby_restaurants(
        db,
        latitude=restaurant.latitude,
        longitude=restaurant.longitude,
        exclude_restaurant_id=restaurant.id,
        radius_km=radius_km,
        limit=limit,
    )
    return {"items": items}


@router.post("/{slug}/refresh-google", response_model=RestaurantResponse)
async def refresh_restaurant_from_google_endpoint(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> Restaurant:
    """Force a synchronous refresh of Google Places enrichment fields.

    Returns 503 when GOOGLE_PLACES_API_KEY is not configured, and 404 when
    the restaurant does not exist or has no google_place_id.
    """
    from app.config import settings as _settings

    if not _settings.GOOGLE_PLACES_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Places API key not configured",
        )

    restaurant = await get_restaurant_by_slug(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    if not restaurant.google_place_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Restaurant has no google_place_id to refresh from",
        )

    updated = await refresh_restaurant_from_google(db, restaurant.id, force=True)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve data from Google Places",
        )

    full = await get_restaurant_detail(db, slug)
    if full is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurant not found"
        )
    return full


@router.post(
    "/{source_id}/merge",
    response_model=RestaurantMergeResponse,
)
async def merge_restaurant(
    source_id: uuid.UUID,
    payload: RestaurantMergeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> dict:
    """Admin: merge `source_id` into `payload.target_id`.

    Moves all dishes, reviews, ratings, diary entries, images and existing
    redirects from source to target, deletes the source row, and inserts a
    redirect from the source's slug. Aggregates on the target are recomputed
    from the new state. Whole operation is one transaction.
    """
    if source_id == payload.target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source_id and target_id must differ",
        )
    try:
        summary = await merge_restaurants(
            db,
            source_id=source_id,
            target_id=payload.target_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return {**summary, "target_id": payload.target_id}


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_restaurant(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> None:
    result = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    await delete_images_for_restaurant(db, restaurant.id)
    await db.delete(restaurant)
    await db.flush()


@router.post(
    "/{slug}/reservation-click",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def log_reservation_click(
    slug: str,
    payload: ReservationClickCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """Logs a click on the "Reservar mesa" CTA. Auth opcional.

    Devuelve 404 si el restaurant no tiene `reservation_url` configurada para
    evitar inflar la tabla con eventos de páginas que no muestran el CTA.
    """
    result = await db.execute(select(Restaurant).where(Restaurant.slug == slug))
    restaurant = result.scalar_one_or_none()
    if restaurant is None or not restaurant.reservation_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant has no reservation flow",
        )

    click = ReservationClick(
        restaurant_id=restaurant.id,
        user_id=current_user.id if current_user is not None else None,
        provider=payload.provider or restaurant.reservation_provider,
        referrer=payload.referrer,
        utm=payload.utm,
        session_id=payload.session_id,
    )
    db.add(click)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
