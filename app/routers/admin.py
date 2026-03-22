from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import hash_password, require_role
from app.models.category import Category
from app.models.dish import Dish, DishReview
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole

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
