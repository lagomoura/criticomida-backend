from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.menu import Menu
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.menu import MenuCreate, MenuResponse, MenuUpdate

router = APIRouter(tags=["menus"])


@router.get(
    "/api/restaurants/{slug}/menu",
    response_model=MenuResponse | None,
)
async def get_menu(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Menu | None:
    # Verify restaurant exists
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == slug)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    menu_result = await db.execute(
        select(Menu).where(Menu.restaurant_id == restaurant.id)
    )
    menu = menu_result.scalar_one_or_none()
    if menu is None:
        return None
    return menu


@router.put(
    "/api/restaurants/{slug}/menu",
    response_model=MenuResponse,
)
async def upsert_menu(
    slug: str,
    menu_data: MenuUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[
        User, Depends(require_role(UserRole.admin, UserRole.critic))
    ],
) -> Menu:
    # Verify restaurant exists
    result = await db.execute(
        select(Restaurant).where(Restaurant.slug == slug)
    )
    restaurant = result.scalar_one_or_none()
    if restaurant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found",
        )

    # Check if menu already exists
    menu_result = await db.execute(
        select(Menu).where(Menu.restaurant_id == restaurant.id)
    )
    menu = menu_result.scalar_one_or_none()

    if menu:
        # Update existing menu
        update_data = menu_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(menu, field, value)
    else:
        # Create new menu - require image_url and upload_date
        if menu_data.image_url is None or menu_data.upload_date is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="image_url and upload_date are required when creating a new menu",
            )
        menu = Menu(
            restaurant_id=restaurant.id,
            image_url=menu_data.image_url,
            upload_date=menu_data.upload_date,
        )
        db.add(menu)

    await db.flush()
    await db.refresh(menu)
    return menu
