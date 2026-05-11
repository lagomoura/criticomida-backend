"""Endpoints de descubrimiento — feed por Geek Score, duelo de platos.

`/api/dishes/discover` y `/api/dishes/duel` viven aquí (no en `dishes.py`)
porque son surfaces nuevas con un response shape distinto y conviene tenerlas
agrupadas para mantener delgado el router CRUD de platos.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models.user import User
from app.schemas.discovery import (
    DiscoveryDishPage,
    DishDuelResponse,
    DuelFamiliesResponse,
    DuelRootsResponse,
    PillarKey,
)
from app.services.discovery_service import (
    discover_dishes,
    duel_dishes,
    popular_dish_families,
    popular_dish_roots,
)

router = APIRouter(tags=["discovery"])


@router.get("/api/dishes/discover", response_model=DiscoveryDishPage)
async def discover(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float | None = Query(default=None, ge=0.1, le=50.0),
    sort: Literal[
        "geek_score",
        "execution",
        "value_prop",
        "presentation",
        "distance",
        "nearby_smart",
    ] = Query(default="geek_score"),
    category: str | None = Query(default=None, description="Slug de categoría"),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> DiscoveryDishPage:
    items = await discover_dishes(
        db,
        viewer_id=viewer.id if viewer else None,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        sort=sort,
        category_slug=category,
        limit=limit,
        offset=offset,
    )
    return DiscoveryDishPage(items=items)


@router.get("/api/dishes/duel", response_model=DishDuelResponse)
async def duel(
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    family: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description="Familia semántica (ej. 'burger'). Default del rail nuevo.",
    ),
    root: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
        description="Raíz exacta del plato (ej. 'sorrentinos'). Modo más estricto.",
    ),
    pillar: PillarKey = Query(
        default="value_prop",
        description="Pilar del duelo: value_prop | execution | presentation | overall_rating",
    ),
    category: str | None = Query(
        default=None,
        description="Slug de categoría de restaurante (opcional, filtro adicional).",
    ),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float | None = Query(default=None, ge=0.1, le=50.0),
) -> DishDuelResponse:
    items, fallback_reason = await duel_dishes(
        db,
        viewer_id=viewer.id if viewer else None,
        dish_root=root,
        dish_family=family,
        pillar=pillar,
        category_slug=category,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
    )
    return DishDuelResponse(
        category=category,
        root=root,
        family=family,
        pillar=pillar,
        items=items,
        fallback_reason=fallback_reason,
    )


@router.get("/api/dishes/duel/families", response_model=DuelFamiliesResponse)
async def duel_families(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    category: str | None = Query(
        default=None, description="Slug de categoría de restaurante (filtro opcional)."
    ),
    limit: int = Query(default=20, ge=1, le=50),
    min_restaurants: int = Query(default=2, ge=2, le=20),
    recent_days: int = Query(default=90, ge=1, le=365),
) -> DuelFamiliesResponse:
    """Familias activas (>= min_restaurants contendientes), ordenadas por
    actividad reciente. Cada familia alimenta un slide del carrusel.
    """
    items = await popular_dish_families(
        db,
        category_slug=category,
        limit=limit,
        min_restaurants=min_restaurants,
        recent_days=recent_days,
    )
    response.headers["Cache-Control"] = "public, max-age=300"
    return DuelFamiliesResponse(items=items)


@router.get("/api/dishes/duel/roots", response_model=DuelRootsResponse)
async def duel_roots(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    category: str | None = Query(
        default=None, description="Slug de categoría de restaurante (filtro opcional)."
    ),
    limit: int = Query(default=20, ge=1, le=50),
    min_restaurants: int = Query(default=2, ge=2, le=20),
    recent_days: int = Query(default=90, ge=1, le=365),
) -> DuelRootsResponse:
    """Lista raíces de platos con al menos `min_restaurants` contendientes,
    ordenadas por actividad reciente. Alimenta el selector del Duelo.
    """
    items = await popular_dish_roots(
        db,
        category_slug=category,
        limit=limit,
        min_restaurants=min_restaurants,
        recent_days=recent_days,
    )
    # Cache CDN-friendly: el ranking de raíces no cambia minuto a minuto.
    response.headers["Cache-Control"] = "public, max-age=300"
    return DuelRootsResponse(items=items)
