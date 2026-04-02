from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.category import Category
from app.models.dish import Dish
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.category import CategoryCreate, CategoryResponse, CategoryUpdate

router = APIRouter(prefix="/api/categories", tags=["categories"])


class CategoryWithCount(CategoryResponse):
    restaurant_count: int = 0


@router.get("", response_model=list[CategoryResponse])
async def list_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    review_count_subq = (
        select(func.coalesce(func.sum(Dish.review_count), 0))
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .where(Restaurant.category_id == Category.id)
        .correlate(Category)
        .scalar_subquery()
    )
    result = await db.execute(
        select(Category, review_count_subq.label("review_count")).order_by(
            Category.display_order, Category.name
        )
    )
    rows = result.all()
    return [
        {
            "id": cat.id,
            "slug": cat.slug,
            "name": cat.name,
            "description": cat.description,
            "image_url": cat.image_url,
            "display_order": cat.display_order,
            "review_count": count,
        }
        for cat, count in rows
    ]


@router.get("/{slug}", response_model=CategoryWithCount)
async def get_category(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    result = await db.execute(select(Category).where(Category.slug == slug))
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    # Count restaurants in this category
    count_result = await db.execute(
        select(func.count()).where(Restaurant.category_id == category.id)
    )
    restaurant_count = count_result.scalar_one()

    return {
        "id": category.id,
        "slug": category.slug,
        "name": category.name,
        "description": category.description,
        "image_url": category.image_url,
        "display_order": category.display_order,
        "restaurant_count": restaurant_count,
    }


@router.post("", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_data: CategoryCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> Category:
    # Check slug uniqueness
    existing = await db.execute(
        select(Category).where(Category.slug == category_data.slug)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Category with this slug already exists",
        )

    category = Category(**category_data.model_dump())
    db.add(category)
    await db.flush()
    await db.refresh(category)
    return category


@router.put("/{slug}", response_model=CategoryResponse)
async def update_category(
    slug: str,
    category_data: CategoryUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> Category:
    result = await db.execute(select(Category).where(Category.slug == slug))
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    update_data = category_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(category, field, value)

    await db.flush()
    await db.refresh(category)
    return category


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> None:
    result = await db.execute(select(Category).where(Category.slug == slug))
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    await db.delete(category)
    await db.flush()
