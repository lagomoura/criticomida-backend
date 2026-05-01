"""Endpoints de descubrimiento — feed por Geek Score, duelo de platos.

`/api/dishes/discover` y `/api/dishes/duel` viven aquí (no en `dishes.py`)
porque son surfaces nuevas con un response shape distinto y conviene tenerlas
agrupadas para mantener delgado el router CRUD de platos.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models.user import User
from app.schemas.discovery import (
    DiscoveryDishPage,
    DishDuelResponse,
)
from app.services.discovery_service import (
    discover_dishes,
    duel_dishes,
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
    category: str = Query(..., min_length=1, description="Slug de categoría"),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lng: float | None = Query(default=None, ge=-180, le=180),
    radius_km: float | None = Query(default=None, ge=0.1, le=50.0),
) -> DishDuelResponse:
    items = await duel_dishes(
        db,
        viewer_id=viewer.id if viewer else None,
        category_slug=category,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
    )
    return DishDuelResponse(category=category, items=items)
