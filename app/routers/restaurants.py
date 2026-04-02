import re
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.db_errors import is_unique_violation
from app.middleware.auth import get_current_user, require_role
from app.models.category import Category
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse
from app.schemas.restaurant import (
    RestaurantCreate,
    RestaurantListResponse,
    RestaurantResponse,
    RestaurantUpdate,
)
from app.services.image_cleanup import delete_images_for_restaurant
from app.services.restaurant_service import (
    get_restaurant_detail,
    get_restaurant_list,
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


@router.get("/{slug}", response_model=RestaurantResponse)
async def get_restaurant(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Restaurant:
    restaurant = await get_restaurant_detail(db, slug)
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )
    return restaurant


@router.post("", response_model=RestaurantResponse, status_code=status.HTTP_201_CREATED)
async def create_restaurant(
    restaurant_data: RestaurantCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> Restaurant:
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

    payload = restaurant_data.model_dump(exclude={"slug"})
    base_slug = (restaurant_data.slug or "").strip() or _slugify(
        restaurant_data.name
    )
    max_attempts = 8
    restaurant: Restaurant | None = None

    for attempt in range(max_attempts):
        if attempt == 0:
            slug = base_slug
            taken = await db.execute(
                select(Restaurant.id).where(Restaurant.slug == slug).limit(1)
            )
            if taken.scalar_one_or_none() is not None:
                slug = f"{base_slug}-{uuid.uuid4().hex[:8]}"
        else:
            slug = f"{base_slug}-{uuid.uuid4().hex[:8]}"

        candidate = Restaurant(
            **payload,
            slug=slug,
            created_by=current_user.id,
        )
        db.add(candidate)
        try:
            await db.flush()
            restaurant = candidate
            break
        except IntegrityError as exc:
            await db.rollback()
            if not is_unique_violation(exc):
                raise
            if attempt == max_attempts - 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Could not allocate a unique restaurant slug",
                ) from exc

    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not allocate a unique restaurant slug",
        )

    await db.refresh(restaurant)

    # Reload with relationships
    result = await db.execute(
        select(Restaurant)
        .options(
            selectinload(Restaurant.category),
            selectinload(Restaurant.creator),
        )
        .where(Restaurant.id == restaurant.id)
    )
    return result.scalar_one()


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
