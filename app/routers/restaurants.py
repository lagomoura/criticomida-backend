import re
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.category import Category
from app.models.image import EntityType, Image
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse
from app.schemas.restaurant import (
    RestaurantCreate,
    RestaurantListResponse,
    RestaurantResponse,
    RestaurantUpdate,
)
from app.services.restaurant_service import (
    get_restaurant_detail,
    get_restaurant_gallery_images,
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
    # Verify category exists
    cat_result = await db.execute(
        select(Category).where(Category.id == restaurant_data.category_id)
    )
    if cat_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    # Auto-generate slug if not provided or use provided one
    slug = restaurant_data.slug or _slugify(restaurant_data.name)

    # Check slug uniqueness
    existing = await db.execute(
        select(Restaurant).where(Restaurant.slug == slug)
    )
    if existing.scalar_one_or_none() is not None:
        # Append a short random suffix
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    restaurant = Restaurant(
        **restaurant_data.model_dump(exclude={"slug"}),
        slug=slug,
        created_by=current_user.id,
    )
    db.add(restaurant)
    await db.flush()
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

    await db.flush()

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

    await db.delete(restaurant)
    await db.flush()
