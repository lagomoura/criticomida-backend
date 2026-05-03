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
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import (
    Float,
    Integer,
    case,
    cast,
    desc,
    distinct,
    func,
    literal,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.models.category import Category
from app.models.dish import Dish, DishReview, DishReviewImage, WantToTryDish
from app.models.restaurant import Restaurant
from app.schemas.discovery import (
    DiscoveryDishItem,
    DiscoveryPillarStats,
    MapBboxResponse,
    MapDishHighlight,
    MapRestaurantPin,
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

# --- Badges del mapa ---
# Un plato gana badge si su pilar promedio crudo supera el umbral Y tiene al
# menos N reviews. El min_reviews protege de 1-review wonders. No usamos el
# shrinkage acá porque shrinka demasiado para un threshold binario (ej. 5
# reviews de execution=3 dan shrunk=2.5, que se sentiría como "no califica"
# cuando claramente lo hace).
MIN_REVIEWS_FOR_BADGE = 3
BADGE_AVG_THRESHOLD = 2.7

# --- Trending ---
TRENDING_WINDOW_HOURS = 48

# --- Nearby Smart: ranking compuesto cercanía + ejecución + recencia ---
# Suma ponderada sobre 3 componentes normalizados a [0, 1]. La ejecución técnica
# pesa más que la cercanía (un plato 3★ a 5km le gana a uno 1★ a 500m).
W_NEARBY_PROXIMITY = 2.5
W_NEARBY_EXECUTION = 3.0
W_NEARBY_RECENCY = 1.0


SortKey = Literal[
    "geek_score",
    "execution",
    "value_prop",
    "presentation",
    "distance",
    "nearby_smart",
]
BboxSortKey = Literal["geek_score", "value_prop", "trending"]


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

    # Nearby Smart ranking: solo se materializa cuando hay lat/lng. Si el sort
    # es nearby_smart pero el viewer no tiene geo, caemos a geek_score más abajo.
    nearby_smart_expr: ColumnElement | None = None
    if distance_expr is not None:
        now = datetime.now(timezone.utc)
        proximity_score = case(
            (distance_expr <= 1.0, 1.0),
            (distance_expr <= 5.0, 0.7),
            (distance_expr <= 15.0, 0.3),
            else_=0.0,
        )
        # exec_avg viene en escala 1..3. Si null (sin reviews con execution),
        # damos score 0 — no inflamos platos sin pilar técnico cargado.
        execution_score = func.coalesce((exec_avg - 1.0) / 2.0, 0.0)
        last_review_at = func.max(DishReview.created_at)
        recency_score = case(
            (last_review_at >= now - timedelta(days=7), 1.0),
            (last_review_at >= now - timedelta(days=30), 0.6),
            (last_review_at >= now - timedelta(days=90), 0.3),
            else_=0.1,
        )
        nearby_smart_expr = (
            W_NEARBY_PROXIMITY * proximity_score
            + W_NEARBY_EXECUTION * execution_score
            + W_NEARBY_RECENCY * recency_score
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

    # Fallback de cover: si Dish.cover_image_url es NULL (caso típico cuando el
    # plato solo tiene fotos subidas vía reviews y nadie corrió seed_review_images),
    # caemos a la imagen de review más reciente para que el rail no muestre
    # 'Sin foto' siendo que la galería del plato sí tiene fotos.
    review_cover_expr = (
        select(DishReviewImage.url)
        .join(DishReview, DishReview.id == DishReviewImage.dish_review_id)
        .where(DishReview.dish_id == Dish.id)
        .order_by(
            DishReviewImage.uploaded_at.desc(),
            DishReviewImage.display_order.asc(),
        )
        .limit(1)
        .correlate(Dish)
        .scalar_subquery()
    )
    cover_image_col = func.coalesce(Dish.cover_image_url, review_cover_expr)

    columns = [
        Dish.id.label("dish_id"),
        Dish.name.label("dish_name"),
        cover_image_col.label("cover_image_url"),
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
    elif sort == "nearby_smart" and nearby_smart_expr is not None:
        # Tiebreaker: geek_score para estabilizar orden cuando empatan en
        # priority (puede pasar al cruzarse umbrales escalonados de proximidad).
        stmt = stmt.order_by(desc(nearby_smart_expr), desc(geek_score), Dish.id)
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


async def discover_restaurants_in_bbox(
    db: AsyncSession,
    *,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    limit: int = 200,
    sort: BboxSortKey = "geek_score",
    include_empty: bool = False,
    chef_only: bool = False,
) -> MapBboxResponse:
    """Restaurantes dentro del bbox con su Golden Dish y Best Value precomputados.

    Sort keys:
      - `geek_score` (default): orden por mejor plato del local (top geek score).
      - `value_prop`: por mejor relación precio/calidad del local.
      - `trending`: por cantidad de reviews recientes (últimas 48h) en el local.

    Cuando `include_empty=true`, después de los locales con reviews se
    agregan locales del bbox sin ninguna review (con flag `is_empty=true`),
    pensados para CTAs tipo "sé el primero en reseñar".

    Cuando `chef_only=true`, se filtra a solo restaurantes con al menos un
    plato Chef Badge (`execution_avg ≥ BADGE_AVG_THRESHOLD` y ≥
    `MIN_REVIEWS_FOR_BADGE` reviews). Los locales sin reviews se excluyen
    aun si `include_empty=true`, ya que por definición no pueden tener
    Chef Badge.
    """
    trending_cutoff = datetime.now(timezone.utc) - timedelta(hours=TRENDING_WINDOW_HOURS)

    exec_n = cast(func.count(DishReview.execution), Float)
    exec_avg = func.avg(DishReview.execution)
    val_n = cast(func.count(DishReview.value_prop), Float)
    val_avg = func.avg(DishReview.value_prop)
    pres_n = cast(func.count(DishReview.presentation), Float)
    pres_avg = func.avg(DishReview.presentation)
    stars_n = cast(func.count(DishReview.id), Float)
    stars_avg = func.avg(DishReview.rating)
    trending_n = cast(
        func.count(case((DishReview.created_at >= trending_cutoff, 1))),
        Integer,
    )

    exec_shrunk = _shrink(exec_avg, exec_n, C_PILLAR, PRIOR_PILLAR)
    val_shrunk = _shrink(val_avg, val_n, C_PILLAR, PRIOR_PILLAR)
    pres_shrunk = _shrink(pres_avg, pres_n, C_PILLAR, PRIOR_PILLAR)
    stars_shrunk = _shrink(stars_avg, stars_n, C_STARS, PRIOR_STARS)

    geek_score = (
        W_EXECUTION * (exec_shrunk - 1.0) / 2.0
        + W_VALUE_PROP * (val_shrunk - 1.0) / 2.0
        + W_PRESENTATION * (pres_shrunk - 1.0) / 2.0
        + W_STARS * (stars_shrunk - 1.0) / 4.0
    ) * 100.0

    dish_scores = (
        select(
            Dish.id.label("dish_id"),
            Dish.name.label("dish_name"),
            Dish.cover_image_url.label("cover_image_url"),
            Dish.review_count.label("review_count"),
            Restaurant.id.label("restaurant_id"),
            Restaurant.slug.label("slug"),
            Restaurant.name.label("restaurant_name"),
            Restaurant.latitude.label("latitude"),
            Restaurant.longitude.label("longitude"),
            Restaurant.cover_image_url.label("rest_cover"),
            Restaurant.location_name.label("rest_location"),
            Restaurant.computed_rating.label("rest_rating"),
            Restaurant.review_count.label("rest_review_count"),
            Restaurant.price_level.label("rest_price_level"),
            Restaurant.cuisine_types.label("rest_cuisine_types"),
            Category.name.label("category_name"),
            exec_avg.label("exec_avg"),
            exec_n.label("exec_n"),
            val_avg.label("val_avg"),
            val_n.label("val_n"),
            pres_avg.label("pres_avg"),
            exec_shrunk.label("exec_shrunk"),
            val_shrunk.label("val_shrunk"),
            geek_score.label("geek_score"),
            trending_n.label("trending_n"),
        )
        .join(DishReview, DishReview.dish_id == Dish.id)
        .join(Restaurant, Restaurant.id == Dish.restaurant_id)
        .outerjoin(Category, Category.id == Restaurant.category_id)
        .where(
            Restaurant.latitude.is_not(None),
            Restaurant.longitude.is_not(None),
            Restaurant.latitude.between(min_lat, max_lat),
            Restaurant.longitude.between(min_lng, max_lng),
        )
        .group_by(Dish.id, Restaurant.id, Category.id)
        .cte("dish_scores")
    )

    golden_rk = func.row_number().over(
        partition_by=dish_scores.c.restaurant_id,
        order_by=[
            desc(dish_scores.c.exec_shrunk),
            desc(dish_scores.c.geek_score),
            dish_scores.c.dish_id,
        ],
    )
    value_rk = func.row_number().over(
        partition_by=dish_scores.c.restaurant_id,
        order_by=[
            desc(dish_scores.c.val_shrunk),
            desc(dish_scores.c.geek_score),
            dish_scores.c.dish_id,
        ],
    )
    top_geek = func.max(dish_scores.c.geek_score).over(
        partition_by=dish_scores.c.restaurant_id
    )
    top_val_shrunk = func.max(dish_scores.c.val_shrunk).over(
        partition_by=dish_scores.c.restaurant_id
    )
    top_trending = func.sum(dish_scores.c.trending_n).over(
        partition_by=dish_scores.c.restaurant_id
    )
    is_chef_dish = case(
        (
            (dish_scores.c.exec_avg >= BADGE_AVG_THRESHOLD)
            & (dish_scores.c.exec_n >= MIN_REVIEWS_FOR_BADGE),
            True,
        ),
        else_=False,
    )
    is_gem_dish = case(
        (
            (dish_scores.c.val_avg >= BADGE_AVG_THRESHOLD)
            & (dish_scores.c.val_n >= MIN_REVIEWS_FOR_BADGE),
            True,
        ),
        else_=False,
    )
    has_chef_badge = func.bool_or(is_chef_dish).over(
        partition_by=dish_scores.c.restaurant_id
    )
    has_gem_badge = func.bool_or(is_gem_dish).over(
        partition_by=dish_scores.c.restaurant_id
    )

    ranked = (
        select(
            dish_scores.c.dish_id,
            dish_scores.c.dish_name,
            dish_scores.c.cover_image_url,
            dish_scores.c.review_count,
            dish_scores.c.restaurant_id,
            dish_scores.c.slug,
            dish_scores.c.restaurant_name,
            dish_scores.c.latitude,
            dish_scores.c.longitude,
            dish_scores.c.rest_cover,
            dish_scores.c.rest_location,
            dish_scores.c.rest_rating,
            dish_scores.c.rest_review_count,
            dish_scores.c.rest_price_level,
            dish_scores.c.rest_cuisine_types,
            dish_scores.c.category_name,
            dish_scores.c.exec_avg,
            dish_scores.c.val_avg,
            dish_scores.c.pres_avg,
            dish_scores.c.geek_score,
            golden_rk.label("golden_rk"),
            value_rk.label("value_rk"),
            top_geek.label("top_geek"),
            top_val_shrunk.label("top_val_shrunk"),
            top_trending.label("top_trending"),
            has_chef_badge.label("has_chef_badge"),
            has_gem_badge.label("has_gem_badge"),
        )
        .select_from(dish_scores)
        .cte("ranked")
    )

    # Postgres no tiene MAX(uuid), así que en vez de pivotar con CASE, filtramos
    # dos CTEs (rk=1) y los joineamos por restaurante. Una fila por restaurante.
    goldens = select(ranked).where(ranked.c.golden_rk == 1).cte("goldens")
    bestvals = select(ranked).where(ranked.c.value_rk == 1).cte("bestvals")

    sort_column = {
        "geek_score": desc(goldens.c.top_geek),
        "value_prop": desc(goldens.c.top_val_shrunk),
        "trending": desc(goldens.c.top_trending),
    }[sort]

    final = (
        select(
            goldens.c.restaurant_id,
            goldens.c.slug,
            goldens.c.restaurant_name,
            goldens.c.latitude,
            goldens.c.longitude,
            goldens.c.top_geek,
            goldens.c.top_trending,
            goldens.c.has_chef_badge,
            goldens.c.has_gem_badge,
            goldens.c.rest_cover,
            goldens.c.rest_location,
            goldens.c.rest_rating,
            goldens.c.rest_review_count,
            goldens.c.rest_price_level,
            goldens.c.rest_cuisine_types,
            goldens.c.category_name,
            goldens.c.dish_id.label("golden_id"),
            goldens.c.dish_name.label("golden_name"),
            goldens.c.cover_image_url.label("golden_cover"),
            goldens.c.exec_avg.label("golden_exec"),
            goldens.c.val_avg.label("golden_val"),
            goldens.c.pres_avg.label("golden_pres"),
            goldens.c.review_count.label("golden_n"),
            goldens.c.geek_score.label("golden_geek"),
            bestvals.c.dish_id.label("value_id"),
            bestvals.c.dish_name.label("value_name"),
            bestvals.c.cover_image_url.label("value_cover"),
            bestvals.c.exec_avg.label("value_exec"),
            bestvals.c.val_avg.label("value_val"),
            bestvals.c.pres_avg.label("value_pres"),
            bestvals.c.review_count.label("value_n"),
            bestvals.c.geek_score.label("value_geek"),
        )
        .select_from(
            goldens.outerjoin(
                bestvals, bestvals.c.restaurant_id == goldens.c.restaurant_id
            )
        )
    )
    if chef_only:
        final = final.where(goldens.c.has_chef_badge.is_(True))
    final = final.order_by(
        sort_column, desc(goldens.c.top_geek), goldens.c.restaurant_id
    ).limit(limit)

    rows = (await db.execute(final)).all()
    items = [_row_to_pin(r) for r in rows]

    # Locales sin reviews no pueden tener Chef Badge — los suprimimos cuando
    # se pidió `chef_only`, aun si `include_empty` está activo.
    if include_empty and not chef_only:
        remaining = max(0, limit - len(items))
        if remaining > 0:
            empty_rows = await _fetch_empty_restaurants(
                db,
                min_lat=min_lat,
                min_lng=min_lng,
                max_lat=max_lat,
                max_lng=max_lng,
                limit=remaining,
            )
            items.extend(_row_to_empty_pin(r) for r in empty_rows)

    truncated = len(items) >= limit
    return MapBboxResponse(items=items, truncated=truncated)


async def _fetch_empty_restaurants(
    db: AsyncSession,
    *,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    limit: int,
):
    """Restaurantes en el bbox que no tienen ninguna dish_review.

    Sirve para los pines "Missing Spots" del mapa: el mapa marca el lugar y
    el frontend ofrece un CTA para que el usuario sea el primero en reseñar.
    """
    has_review_subq = (
        select(distinct(Dish.restaurant_id))
        .select_from(Dish)
        .join(DishReview, DishReview.dish_id == Dish.id)
        .scalar_subquery()
    )
    stmt = (
        select(
            Restaurant.id.label("restaurant_id"),
            Restaurant.slug,
            Restaurant.name.label("restaurant_name"),
            Restaurant.latitude,
            Restaurant.longitude,
            Restaurant.cover_image_url.label("rest_cover"),
            Restaurant.location_name.label("rest_location"),
            Restaurant.computed_rating.label("rest_rating"),
            Restaurant.review_count.label("rest_review_count"),
            Restaurant.price_level.label("rest_price_level"),
            Restaurant.cuisine_types.label("rest_cuisine_types"),
            Category.name.label("category_name"),
        )
        .outerjoin(Category, Category.id == Restaurant.category_id)
        .where(
            Restaurant.latitude.is_not(None),
            Restaurant.longitude.is_not(None),
            Restaurant.latitude.between(min_lat, max_lat),
            Restaurant.longitude.between(min_lng, max_lng),
            ~Restaurant.id.in_(has_review_subq),
        )
        .order_by(Restaurant.name, Restaurant.id)
        .limit(limit)
    )
    return (await db.execute(stmt)).all()


def _row_to_pin(row) -> MapRestaurantPin:
    golden = (
        MapDishHighlight(
            dish_id=row.golden_id,
            name=row.golden_name,
            cover_image_url=row.golden_cover,
            execution_avg=_round_or_none(row.golden_exec),
            value_prop_avg=_round_or_none(row.golden_val),
            presentation_avg=_round_or_none(row.golden_pres),
            review_count=int(row.golden_n or 0),
            geek_score=round(float(row.golden_geek or 0.0), 2),
        )
        if row.golden_id is not None
        else None
    )
    best_value = (
        MapDishHighlight(
            dish_id=row.value_id,
            name=row.value_name,
            cover_image_url=row.value_cover,
            execution_avg=_round_or_none(row.value_exec),
            value_prop_avg=_round_or_none(row.value_val),
            presentation_avg=_round_or_none(row.value_pres),
            review_count=int(row.value_n or 0),
            geek_score=round(float(row.value_geek or 0.0), 2),
        )
        if row.value_id is not None
        else None
    )
    return MapRestaurantPin(
        restaurant_id=row.restaurant_id,
        slug=row.slug,
        name=row.restaurant_name,
        latitude=float(row.latitude),
        longitude=float(row.longitude),
        top_geek_score=round(float(row.top_geek or 0.0), 2),
        has_chef_badge=bool(row.has_chef_badge),
        has_gem_badge=bool(row.has_gem_badge),
        cover_image_url=row.rest_cover,
        location_name=row.rest_location,
        computed_rating=round(float(row.rest_rating or 0.0), 2),
        review_count=int(row.rest_review_count or 0),
        price_level=int(row.rest_price_level) if row.rest_price_level is not None else None,
        cuisine_types=list(row.rest_cuisine_types) if row.rest_cuisine_types else None,
        category_name=row.category_name,
        trending_count=int(row.top_trending or 0),
        is_empty=False,
        golden_dish=golden,
        best_value_dish=best_value,
    )


def _row_to_empty_pin(row) -> MapRestaurantPin:
    return MapRestaurantPin(
        restaurant_id=row.restaurant_id,
        slug=row.slug,
        name=row.restaurant_name,
        latitude=float(row.latitude),
        longitude=float(row.longitude),
        top_geek_score=0.0,
        has_chef_badge=False,
        has_gem_badge=False,
        cover_image_url=row.rest_cover,
        location_name=row.rest_location,
        computed_rating=round(float(row.rest_rating or 0.0), 2),
        review_count=int(row.rest_review_count or 0),
        price_level=int(row.rest_price_level) if row.rest_price_level is not None else None,
        cuisine_types=list(row.rest_cuisine_types) if row.rest_cuisine_types else None,
        category_name=row.category_name,
        trending_count=0,
        is_empty=True,
        golden_dish=None,
        best_value_dish=None,
    )
