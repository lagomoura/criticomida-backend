import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.image_cleanup import delete_images_for_dish
from app.middleware.auth import get_current_user, require_role
from app.models.dish import Dish
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.dish import DishCreate, DishResponse, DishUpdate
from app.services.dish_service import get_dish_by_id, get_dish_detail, get_dishes_for_restaurant

router = APIRouter(tags=["dishes"])


class DishSearchItem(BaseModel):
    id: uuid.UUID
    name: str


class DishSearchPage(BaseModel):
    items: list[DishSearchItem]


@router.get("/api/dishes/search", response_model=DishSearchPage)
async def search_dishes(
    db: Annotated[AsyncSession, Depends(get_db)],
    restaurant_place_id: str = Query(min_length=1, max_length=200),
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=10, ge=1, le=50),
) -> DishSearchPage:
    """
    Compose autocomplete for dishes within a given Places-identified restaurant.

    Returns an empty list when the restaurant hasn't been stored yet (brand-new
    place_id) — the frontend falls back to its "crear nuevo plato" affordance
    without an explicit 404.
    """
    restaurant = (
        await db.execute(
            select(Restaurant).where(Restaurant.google_place_id == restaurant_place_id)
        )
    ).scalar_one_or_none()
    if restaurant is None:
        return DishSearchPage(items=[])

    stmt = select(Dish).where(Dish.restaurant_id == restaurant.id)
    if q.strip():
        stmt = stmt.where(Dish.name.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(Dish.review_count.desc(), Dish.name.asc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    return DishSearchPage(
        items=[DishSearchItem(id=d.id, name=d.name) for d in rows]
    )


@router.get(
    "/api/restaurants/{restaurant_slug}/dishes",
    response_model=list[DishResponse],
)
async def list_dishes(
    restaurant_slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[Dish]:
    from app.services.restaurant_service import get_restaurant_by_slug

    # Verify restaurant exists (accepts slug or UUID)
    restaurant = await get_restaurant_by_slug(db, restaurant_slug)
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
    from app.services.restaurant_service import get_restaurant_by_slug

    # Verify restaurant exists (accepts slug or UUID)
    restaurant = await get_restaurant_by_slug(db, restaurant_slug)
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

    await delete_images_for_dish(db, dish.id)
    await db.delete(dish)
    await db.flush()
