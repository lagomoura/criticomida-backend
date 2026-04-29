"""Servicio de descubrimiento: Geek Score + filtros geo + duelo de platos.

El Geek Score combina las estrellas (1..5) con los 3 pilares técnicos
(presentation, value_prop, execution, escala 1..3) en un único ranking
0..100. Aplica shrinkage bayesiano para evitar que platos con 1 sola
reseña dominen el feed.

Todos los cálculos viven en SQL — no se denormaliza una columna geek_score
en `dishes` porque las reseñas se editan/borran y la query es barata
(agregados sobre `dish_reviews(dish_id)` indexado).

Importante: el `n` para el shrinkage de cada pilar es `COUNT(pillar)` (los
pilares son nullable). Si usáramos el `n` total de reseñas, los platos
cuyos reviewers no rellenaron pilares se sobre-shrinkan.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import (
    Float,
    case,
    cast,
    desc,
    func,
    literal,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.models.category import Category
from app.models.dish import Dish, DishReview, WantToTryDish
from app.models.restaurant import Restaurant
from app.schemas.discovery import (
    DiscoveryDishItem,
    DiscoveryPillarStats,
)
from app.services._geo import haversine_km_expr


# --- Geek Score: pesos y priors ---
W_EXECUTION = 0.45
W_VALUE_PROP = 0.25
W_PRESENTATION = 0.15
W_STARS = 0.15

C_PILLAR = 5.0
PRIOR_PILLAR = 2.0  # neutro en la escala 1..3
C_STARS = 5.0
PRIOR_STARS = 3.5  # un poco optimista — "ok" en escala 1..5


SortKey = Literal["geek_score", "execution", "value_prop", "presentation", "distance"]


def _shrink(avg: ColumnElement, n: ColumnElement, c: float, prior: float) -> ColumnElement:
    """(avg·n + C·prior) / (n + C). Con n=0 colapsa a `prior`."""
    return (func.coalesce(avg, 0.0) * n + c * prior) / (n + c)


async def discover_dishes(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    sort: SortKey = "geek_score",
    category_slug: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[DiscoveryDishItem]:
    """Lista de platos rankeados según `sort`, opcionalmente filtrada por geo + categoría.

    - Solo incluye platos con al menos 1 reseña (INNER JOIN a dish_reviews).
    - Cuando `lat`/`lng` están y `radius_km` también, filtra por radio Haversine en SQL.
    - El flag `want_to_try` se hidrata por viewer (false para anónimos).
    """
    # Agregados por plato sobre dish_reviews. NULL pillars salen del COUNT.
    exec_n = cast(func.count(DishReview.execution), Float)
    exec_avg = func.avg(DishReview.execution)
    val_n = cast(func.count(DishReview.value_prop), Float)
    val_avg = func.avg(DishReview.value_prop)
    pres_n = cast(func.count(DishReview.presentation), Float)
    pres_avg = func.avg(DishReview.presentation)
    stars_n = cast(func.count(DishReview.id), Float)
    stars_avg = func.avg(DishReview.rating)

    exec_shrunk = _shrink(exec_avg, exec_n, C_PILLAR, PRIOR_PILLAR)
    val_shrunk = _shrink(val_avg, val_n, C_PILLAR, PRIOR_PILLAR)
    pres_shrunk = _shrink(pres_avg, pres_n, C_PILLAR, PRIOR_PILLAR)
    stars_shrunk = _shrink(stars_avg, stars_n, C_STARS, PRIOR_STARS)

    exec_norm = (exec_shrunk - 1.0) / 2.0
    val_norm = (val_shrunk - 1.0) / 2.0
    pres_norm = (pres_shrunk - 1.0) / 2.0
    stars_norm = (stars_shrunk - 1.0) / 4.0

    geek_score = (
        W_EXECUTION * exec_norm
        + W_VALUE_PROP * val_norm
        + W_PRESENTATION * pres_norm
        + W_STARS * stars_norm
    ) * 100.0

    distance_expr: ColumnElement | None = None
    if lat is not None and lng is not None:
        distance_expr = haversine_km_expr(
            Restaurant.latitude, Restaurant.longitude, lat=lat, lng=lng
        )

    # want_to_try por viewer.
    if viewer_id is not None:
        want_to_try_expr = (
            select(literal(True))
            .select_from(WantToTryDish)
            .where(
                WantToTryDish.user_id == viewer_id,
                WantToTryDish.dish_id == Dish.id,
            )
            .correlate(Dish)
            .scalar_subquery()
        )
        want_to_try_col = func.coalesce(want_to_try_expr, literal(False))
    else:
        want_to_try_col = literal(False)

    columns = [
        Dish.id.label("dish_id"),
        Dish.name.label("dish_name"),
        Dish.cover_image_url,
        Dish.price_tier,
        Dish.computed_rating,
        Dish.review_count,
        Restaurant.id.label("restaurant_id"),
        Restaurant.slug.label("restaurant_slug"),
        Restaurant.name.label("restaurant_name"),
        Restaurant.city.label("restaurant_city"),
        Category.name.label("category_name"),
        exec_avg.label("exec_avg"),
        exec_n.label("exec_n"),
        val_avg.label("val_avg"),
        val_n.label("val_n"),
        pres_avg.label("pres_avg"),
        pres_n.label("pres_n"),
        geek_score.label("geek_score"),
        want_to_try_col.label("want_to_try"),
    ]
    if distance_expr is not None:
        columns.append(distance_expr.label("distance_km"))

    stmt = (
        select(*columns)
        .join(DishReview, DishReview.dish_id == Dish.id)
        .join(Restaurant, Restaurant.id == Dish.restaurant_id)
        .outerjoin(Category, Category.id == Restaurant.category_id)
        .group_by(Dish.id, Restaurant.id, Category.id)
    )

    if category_slug is not None:
        stmt = stmt.where(Category.slug == category_slug)

    if distance_expr is not None and radius_km is not None:
        # Filtro de radio empujado al SQL (no post-filtramos en Python).
        stmt = stmt.where(
            Restaurant.latitude.is_not(None),
            Restaurant.longitude.is_not(None),
        )
        stmt = stmt.having(distance_expr <= radius_km)

    sort_map: dict[SortKey, ColumnElement] = {
        "geek_score": geek_score,
        "execution": exec_shrunk,
        "value_prop": val_shrunk,
        "presentation": pres_shrunk,
    }
    if sort == "distance" and distance_expr is not None:
        stmt = stmt.order_by(distance_expr.asc(), desc(geek_score))
    else:
        primary = sort_map.get(sort, geek_score)
        # Tiebreaker: stars shrunk para estabilizar orden.
        stmt = stmt.order_by(desc(primary), desc(stars_shrunk), Dish.id)

    stmt = stmt.limit(limit).offset(offset)

    rows = (await db.execute(stmt)).all()
    return [_row_to_item(r, has_distance=distance_expr is not None) for r in rows]


async def duel_dishes(
    db: AsyncSession,
    *,
    viewer_id: uuid.UUID | None,
    category_slug: str,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
) -> list[DiscoveryDishItem]:
    """Top 2 platos de la categoría, rankeados por costo/beneficio (value_prop)."""
    return await discover_dishes(
        db,
        viewer_id=viewer_id,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        sort="value_prop",
        category_slug=category_slug,
        limit=2,
        offset=0,
    )


def _row_to_item(row, *, has_distance: bool) -> DiscoveryDishItem:
    return DiscoveryDishItem(
        dish_id=row.dish_id,
        dish_name=row.dish_name,
        cover_image_url=row.cover_image_url,
        price_tier=row.price_tier.value if row.price_tier is not None else None,
        computed_rating=row.computed_rating,
        review_count=row.review_count,
        geek_score=round(float(row.geek_score) if row.geek_score is not None else 0.0, 2),
        pillars=DiscoveryPillarStats(
            presentation_avg=_round_or_none(row.pres_avg),
            presentation_n=int(row.pres_n or 0),
            value_prop_avg=_round_or_none(row.val_avg),
            value_prop_n=int(row.val_n or 0),
            execution_avg=_round_or_none(row.exec_avg),
            execution_n=int(row.exec_n or 0),
        ),
        distance_km=(
            round(float(row.distance_km), 2)
            if has_distance and row.distance_km is not None
            else None
        ),
        restaurant_id=row.restaurant_id,
        restaurant_slug=row.restaurant_slug,
        restaurant_name=row.restaurant_name,
        restaurant_city=row.restaurant_city,
        category=row.category_name,
        want_to_try=bool(row.want_to_try),
    )


def _round_or_none(v) -> float | None:
    if v is None:
        return None
    return round(float(v), 2)
