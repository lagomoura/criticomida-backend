"""Public + authenticated endpoints for dish lists ("rutas").

- ``GET /api/lists/{slug}`` — public read for any list with
  ``is_public=true``. Anonymous OK.
- ``GET /api/lists/me`` — current user's lists.
- ``DELETE /api/lists/{list_id}`` — delete one of my lists.
- ``PATCH /api/lists/{list_id}`` — flip ``is_public`` or rename.

Lists are typically created by the chatbot via the ``create_dish_route``
tool, so we don't need a public ``POST`` endpoint here. A user-driven
"create list" UI will land in a later phase.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.dish import Dish
from app.models.dish_list import DishList, DishListItem
from app.models.restaurant import Restaurant
from app.models.user import User


router = APIRouter(prefix="/api/lists", tags=["lists"])


# ──────────────────────────────────────────────────────────────────────────
#   Schemas
# ──────────────────────────────────────────────────────────────────────────


class DishListItemOut(BaseModel):
    dish_id: uuid.UUID
    position: int
    note: str | None
    dish_name: str
    dish_cover_image_url: str | None
    dish_rating: float | None
    dish_review_count: int
    dish_price_tier: str | None
    restaurant_id: uuid.UUID
    restaurant_slug: str
    restaurant_name: str
    restaurant_location_name: str
    restaurant_lat: float | None
    restaurant_lng: float | None


class DishListOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    is_public: bool
    owner_display_name: str | None
    owner_handle: str | None
    created_at: datetime
    updated_at: datetime
    items: list[DishListItemOut]


class DishListSummaryOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    is_public: bool
    item_count: int
    created_at: datetime
    updated_at: datetime


class DishListPatch(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=160)
    description: str | None = Field(default=None, max_length=600)
    is_public: bool | None = None


# ──────────────────────────────────────────────────────────────────────────
#   Serialization
# ──────────────────────────────────────────────────────────────────────────


def _serialize_list(
    dish_list: DishList,
    *,
    item_rows: list[tuple[DishListItem, Dish, Restaurant]],
    owner: User | None,
) -> DishListOut:
    items: list[DishListItemOut] = []
    for item, dish, rest in item_rows:
        items.append(
            DishListItemOut(
                dish_id=item.dish_id,
                position=item.position,
                note=item.note,
                dish_name=dish.name,
                dish_cover_image_url=dish.cover_image_url,
                dish_rating=(
                    float(dish.computed_rating)
                    if dish.computed_rating is not None
                    else None
                ),
                dish_review_count=dish.review_count,
                dish_price_tier=(
                    dish.price_tier.value if dish.price_tier else None
                ),
                restaurant_id=rest.id,
                restaurant_slug=rest.slug,
                restaurant_name=rest.name,
                restaurant_location_name=rest.location_name,
                restaurant_lat=(
                    float(rest.latitude) if rest.latitude is not None else None
                ),
                restaurant_lng=(
                    float(rest.longitude) if rest.longitude is not None else None
                ),
            )
        )

    return DishListOut(
        id=dish_list.id,
        slug=dish_list.slug,
        name=dish_list.name,
        description=dish_list.description,
        is_public=dish_list.is_public,
        owner_display_name=owner.display_name if owner else None,
        owner_handle=owner.handle if owner else None,
        created_at=dish_list.created_at,
        updated_at=dish_list.updated_at,
        items=items,
    )


async def _load_list_full(
    db: AsyncSession, *, slug: str | None = None, list_id: uuid.UUID | None = None
) -> tuple[DishList, list[tuple[DishListItem, Dish, Restaurant]], User | None] | None:
    if slug is not None:
        stmt = select(DishList).where(DishList.slug == slug)
    elif list_id is not None:
        stmt = select(DishList).where(DishList.id == list_id)
    else:
        return None
    dish_list = (await db.execute(stmt)).scalars().first()
    if dish_list is None:
        return None

    items_stmt = (
        select(DishListItem, Dish, Restaurant)
        .join(Dish, DishListItem.dish_id == Dish.id)
        .join(Restaurant, Dish.restaurant_id == Restaurant.id)
        .where(DishListItem.list_id == dish_list.id)
        .order_by(DishListItem.position.asc())
    )
    rows = list((await db.execute(items_stmt)).all())
    items: list[tuple[DishListItem, Dish, Restaurant]] = [
        (r[0], r[1], r[2]) for r in rows
    ]

    owner = (
        await db.execute(
            select(User).where(User.id == dish_list.owner_user_id)
        )
    ).scalars().first()

    return dish_list, items, owner


# ──────────────────────────────────────────────────────────────────────────
#   Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=list[DishListSummaryOut])
async def my_lists(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> list[DishListSummaryOut]:
    stmt = (
        select(DishList)
        .where(DishList.owner_user_id == user.id)
        .options(selectinload(DishList.items))
        .order_by(DishList.created_at.desc())
    )
    rows = list((await db.execute(stmt)).scalars().unique().all())
    return [
        DishListSummaryOut(
            id=l.id,
            slug=l.slug,
            name=l.name,
            description=l.description,
            is_public=l.is_public,
            item_count=len(l.items),
            created_at=l.created_at,
            updated_at=l.updated_at,
        )
        for l in rows
    ]


@router.get("/{slug}", response_model=DishListOut)
async def get_public_list(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DishListOut:
    """Read a list by slug. Public lists are visible to everyone;
    private lists return 404 to anonymous callers (don't leak existence).
    """
    loaded = await _load_list_full(db, slug=slug)
    if loaded is None:
        raise HTTPException(status_code=404, detail="List not found")
    dish_list, items, owner = loaded
    if not dish_list.is_public:
        raise HTTPException(status_code=404, detail="List not found")
    return _serialize_list(dish_list, item_rows=items, owner=owner)


@router.patch("/{list_id}", response_model=DishListOut)
async def patch_list(
    list_id: uuid.UUID,
    body: DishListPatch,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> DishListOut:
    dish_list = (
        await db.execute(select(DishList).where(DishList.id == list_id))
    ).scalars().first()
    if dish_list is None or dish_list.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="List not found")
    if body.name is not None:
        dish_list.name = body.name
    if body.description is not None:
        dish_list.description = body.description
    if body.is_public is not None:
        dish_list.is_public = body.is_public
    await db.flush()

    loaded = await _load_list_full(db, list_id=list_id)
    assert loaded is not None
    dish_list, items, owner = loaded
    return _serialize_list(dish_list, item_rows=items, owner=owner)


@router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_list(
    list_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    dish_list = (
        await db.execute(select(DishList).where(DishList.id == list_id))
    ).scalars().first()
    if dish_list is None or dish_list.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="List not found")
    await db.delete(dish_list)
    await db.flush()
    return None
