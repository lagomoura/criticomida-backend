import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, require_role
from app.models.dish import Dish
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.dish import DishCreate, DishResponse, DishUpdate
from app.services.dish_service import get_dish_by_id, get_dish_detail, get_dishes_for_restaurant

router = APIRouter(tags=["dishes"])


@router.get(
    "/api/restaurants/{restaurant_slug}/dishes",
    response_model=list[DishResponse],
)
async def list_dishes(
    restaurant_slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Dish]:
    # Verify restaurant exists
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == restaurant_slug)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    return await get_dishes_for_restaurant(db, restaurant.id)


@router.get("/api/dishes/{dish_id}", response_model=DishResponse)
async def get_dish(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Dish:
    dish = await get_dish_detail(db, dish_id)
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )
    return dish


@router.post(
    "/api/restaurants/{restaurant_slug}/dishes",
    response_model=DishResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dish(
    restaurant_slug: str,
    dish_data: DishCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> Dish:
    # Verify restaurant exists
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == restaurant_slug)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    dish = Dish(
        **dish_data.model_dump(exclude={"restaurant_id"}),
        restaurant_id=restaurant.id,
        created_by=current_user.id,
    )
    db.add(dish)
    await db.flush()
    await db.refresh(dish)
    return dish


@router.put("/api/dishes/{dish_id}", response_model=DishResponse)
async def update_dish(
    dish_id: uuid.UUID,
    dish_data: DishUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> Dish:
    dish = await get_dish_by_id(db, dish_id)
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )

    update_data = dish_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(dish, field, value)

    await db.flush()
    await db.refresh(dish)
    return dish


@router.delete("/api/dishes/{dish_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dish(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> None:
    dish = await get_dish_by_id(db, dish_id)
    if dish is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dish not found",
        )

    await db.delete(dish)
    await db.flush()
