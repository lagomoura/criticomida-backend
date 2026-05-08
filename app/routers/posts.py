"""Social compose endpoint: POST /api/posts.

Higher-level than the legacy `POST /api/dishes/{id}/reviews` — accepts free-form
restaurant and dish names and creates the supporting entities on the fly. The
response is the same `FeedItem` shape the rest of the social UI consumes.
"""

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import POST_CREATE_LIMIT, limiter
from app.models.category import Category
from app.models.dish import (
    Dish,
    DishReview,
    DishReviewImage,
    DishReviewProsCons,
    DishReviewProsConsType,
    DishReviewTag,
    MealPeriod as ModelMealPeriod,
    PortionSize as ModelPortionSize,
    PriceTier as ModelPriceTier,
)
from app.models.restaurant import Restaurant
from app.models.user import User
from app.routers.feed import _build_feed_items
from app.schemas.feed import FeedItem
from app.schemas.post_create import PostCreate, RestaurantFromPlace
from app.services.embeddings_service import schedule_reembed_review
from app.services.notification_service import record_mention_notifications
from app.services.price_validation import (
    evaluate_price_outlier,
    validate_price_paid,
)
from app.services.rating_service import (
    update_dish_rating,
    update_restaurant_rating,
)
from app.services.restaurant_service import find_restaurant_by_place_id

router = APIRouter(prefix="/api/posts", tags=["posts"])


_PRICE_TIER_MAP = {
    "$": ModelPriceTier.low,
    "$$": ModelPriceTier.mid,
    "$$$": ModelPriceTier.high,
}


def _slugify(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-") or "sin-nombre"


async def _find_category_id(db: AsyncSession, category_value: str | None) -> int | None:
    """Resuelve un valor del dropdown de compose a `categories.id`.

    El form envía el slug canónico (ej. `italiana`); como fallback admitimos
    el `name` en cualquier locale (ej. "Italiana") para tolerar pre-fills
    legacy donde la API devuelve el display name del Category.
    """
    if not category_value:
        return None
    cleaned = category_value.strip()
    if not cleaned:
        return None
    by_slug = await db.execute(
        select(Category.id).where(Category.slug == cleaned.lower())
    )
    cat_id = by_slug.scalar_one_or_none()
    if cat_id is not None:
        return cat_id
    by_name = await db.execute(
        select(Category.id).where(func.lower(Category.name) == cleaned.lower())
    )
    return by_name.scalar_one_or_none()


async def _unique_slug_for(db: AsyncSession, base: str) -> str:
    """Return `base` if the slug is free, otherwise suffix with a short uuid."""
    slug = base
    for _ in range(6):
        clash = await db.execute(
            select(Restaurant.id).where(Restaurant.slug == slug)
        )
        if clash.scalar_one_or_none() is None:
            return slug
        slug = f"{base}-{uuid.uuid4().hex[:6]}"
    raise HTTPException(status_code=500, detail="No se pudo generar un slug")


async def _find_or_create_restaurant_by_name(
    db: AsyncSession,
    *,
    name: str,
    category_id: int | None,
    created_by: uuid.UUID,
) -> Restaurant:
    """Legacy path: match by lowercased name, create with minimal data."""
    cleaned = name.strip()
    result = await db.execute(
        select(Restaurant).where(func.lower(Restaurant.name) == cleaned.lower())
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    slug = await _unique_slug_for(db, _slugify(cleaned))
    restaurant = Restaurant(
        slug=slug,
        name=cleaned,
        location_name="",
        category_id=category_id,
        created_by=created_by,
    )
    db.add(restaurant)
    await db.flush()
    return restaurant


async def _find_or_create_restaurant_from_place(
    db: AsyncSession,
    *,
    place: "RestaurantFromPlace",
    category_id: int | None,
    created_by: uuid.UUID,
) -> Restaurant:
    """Primary path: dedupe by google_place_id."""
    existing = await find_restaurant_by_place_id(db, place.place_id)
    if existing is not None:
        return existing

    cleaned_name = place.name.strip()
    slug = await _unique_slug_for(db, _slugify(cleaned_name))

    restaurant = Restaurant(
        slug=slug,
        name=cleaned_name,
        location_name=(place.formatted_address or "").strip(),
        city=(place.city.strip() if place.city else None) or None,
        latitude=Decimal(str(place.latitude)) if place.latitude is not None else None,
        longitude=Decimal(str(place.longitude)) if place.longitude is not None else None,
        category_id=category_id,
        google_place_id=place.place_id,
        google_maps_url=place.google_maps_url,
        website=place.website,
        phone_number=place.phone_number,
        created_by=created_by,
    )
    db.add(restaurant)
    await db.flush()
    return restaurant


async def _resolve_dish(
    db: AsyncSession,
    *,
    restaurant_id: uuid.UUID,
    dish_id: uuid.UUID | None,
    name: str,
    created_by: uuid.UUID,
    price_tier: ModelPriceTier | None,
) -> Dish:
    """
    Prefer an explicit `dish_id` when the frontend picked an existing dish
    from autocomplete. Falls back to find-or-create by name. The dish must
    belong to the given restaurant either way.
    """
    cleaned = name.strip()

    if dish_id is not None:
        result = await db.execute(select(Dish).where(Dish.id == dish_id))
        dish = result.scalar_one_or_none()
        if dish is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="El plato seleccionado ya no existe.",
            )
        if dish.restaurant_id != restaurant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El plato no pertenece al restaurante indicado.",
            )
        if price_tier is not None and dish.price_tier is None:
            dish.price_tier = price_tier
        return dish

    # Find or create by normalized name. `name_normalized` is a generated
    # column (lower + unaccent + collapsed whitespace) so it matches the
    # rule used by the unique index.
    result = await db.execute(
        select(Dish).where(
            Dish.restaurant_id == restaurant_id,
            Dish.name_normalized == func.dish_name_normalized(cleaned),
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        if price_tier is not None and existing.price_tier is None:
            existing.price_tier = price_tier
        return existing

    dish = Dish(
        restaurant_id=restaurant_id,
        name=cleaned,
        price_tier=price_tier,
        created_by=created_by,
    )
    db.add(dish)
    await db.flush()
    return dish


@router.post(
    "", response_model=FeedItem, status_code=status.HTTP_201_CREATED
)
@limiter.limit(POST_CREATE_LIMIT)
async def create_post(
    request: Request,
    payload: PostCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FeedItem:
    extras = payload.extras

    category_id = await _find_category_id(db, payload.category)

    if payload.restaurant is not None:
        restaurant = await _find_or_create_restaurant_from_place(
            db,
            place=payload.restaurant,
            category_id=category_id,
            created_by=current_user.id,
        )
    else:
        # Legacy caller that still sends free text. `PostCreate` already
        # validated that at least one path is present.
        assert payload.restaurant_name is not None
        restaurant = await _find_or_create_restaurant_by_name(
            db,
            name=payload.restaurant_name,
            category_id=category_id,
            created_by=current_user.id,
        )

    price_tier_model: ModelPriceTier | None = None
    if extras and extras.price_tier:
        price_tier_model = _PRICE_TIER_MAP.get(extras.price_tier)

    dish = await _resolve_dish(
        db,
        restaurant_id=restaurant.id,
        dish_id=payload.dish_id,
        name=payload.dish_name,
        created_by=current_user.id,
        price_tier=price_tier_model,
    )

    portion_model: ModelPortionSize | None = None
    if extras and extras.portion_size:
        portion_model = ModelPortionSize(extras.portion_size)

    # Cap del precio según la moneda del restaurante. El restaurante puede ser
    # recién creado en este request — en ese caso `currency_code` es NULL y
    # cae al rango fallback amplio.
    price_paid_value = extras.price_paid if extras else None
    validate_price_paid(price_paid_value, restaurant.currency_code)
    # Capa 2: outlier vs histórico del plato. Soft-flag, no rechaza.
    price_flagged_at, price_flag_reason = await evaluate_price_outlier(
        db, dish_id=dish.id, price_paid=price_paid_value,
    )

    meal_period_value = None
    if extras and extras.meal_period:
        meal_period_value = ModelMealPeriod(extras.meal_period)

    review = DishReview(
        dish_id=dish.id,
        user_id=current_user.id,
        date_tasted=(extras.date_tasted if extras and extras.date_tasted else date.today()),
        time_tasted=(extras.time_tasted if extras else None),
        meal_period=meal_period_value,
        note=payload.text.strip(),
        rating=payload.score,
        price_paid=price_paid_value,
        price_flagged_at=price_flagged_at,
        price_flag_reason=price_flag_reason,
        portion_size=portion_model,
        would_order_again=(extras.would_order_again if extras else None),
        visited_with=(extras.visited_with if extras else None),
        is_anonymous=bool(extras.is_anonymous) if extras else False,
        presentation=(extras.presentation if extras else None),
        value_prop=(extras.value_prop if extras else None),
        execution=(extras.execution if extras else None),
    )
    db.add(review)
    await db.flush()  # need review.id for pros/cons/tags

    if extras:
        for text in extras.pros:
            stripped = text.strip()
            if stripped:
                db.add(
                    DishReviewProsCons(
                        dish_review_id=review.id,
                        type=DishReviewProsConsType.pro,
                        text=stripped,
                    )
                )
        for text in extras.cons:
            stripped = text.strip()
            if stripped:
                db.add(
                    DishReviewProsCons(
                        dish_review_id=review.id,
                        type=DishReviewProsConsType.con,
                        text=stripped,
                    )
                )
        for raw in extras.tags:
            stripped = raw.strip()
            if stripped:
                db.add(DishReviewTag(dish_review_id=review.id, tag=stripped))
        for img in extras.images:
            db.add(
                DishReviewImage(
                    dish_review_id=review.id,
                    url=img.url,
                    alt_text=img.alt_text,
                    display_order=img.display_order,
                )
            )

    await update_dish_rating(db, dish.id)
    await update_restaurant_rating(db, restaurant.id)

    skip = {restaurant.claimed_by_user_id} if restaurant.claimed_by_user_id else set()
    await record_mention_notifications(
        db,
        actor_id=current_user.id,
        body=review.note or "",
        target_kind="post",
        target_review_id=review.id,
        skip_recipient_ids=skip,
    )

    await db.flush()
    await schedule_reembed_review(db, review.id)

    # Rehydrate the FeedItem using the shared feed helper so the response shape
    # matches the rest of the social UI (counts, viewer_state, etc.).
    items, _ = await _build_feed_items(
        db,
        current_user,
        base_filters=[DishReview.id == review.id],
        cursor_dt=None,
        limit=1,
        with_extras=True,
    )
    if not items:
        # Should be unreachable — we just inserted the row.
        raise HTTPException(status_code=500, detail="No se pudo hidratar el post creado")
    return items[0]
