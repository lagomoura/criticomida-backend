import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.image_cleanup import delete_images_for_dish
from app.middleware.auth import get_current_user, require_role
from app.models.dish import Dish
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.dish import (
    DishCreate,
    DishMergeRequest,
    DishMergeResponse,
    DishResponse,
    DishUpdate,
)
from app.services.dish_service import (
    get_dish_by_id,
    get_dish_detail,
    get_dishes_for_restaurant,
    merge_dishes,
)

router = APIRouter(tags=["dishes"])


class DishSearchItem(BaseModel):
    id: uuid.UUID
    name: str


class DishSearchPage(BaseModel):
    items: list[DishSearchItem]


class DishSuggestion(BaseModel):
    id: uuid.UUID
    name: str
    review_count: int
    similarity: float
    is_exact_normalized: bool


class DishSuggestionPage(BaseModel):
    items: list[DishSuggestion]
    """
    Empty when the input has no plausible duplicate. The frontend uses that as
    the green light to create a new dish silently. When non-empty, the modal
    lists candidates so the user can pick instead of creating a duplicate.
    """


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
    cleaned_q = q.strip()
    if cleaned_q:
        # Match either the raw name (legacy ILIKE for partial words) or the
        # normalized form, so "Muzza" finds "Muzzá" / "Muzzá " and "cafe"
        # finds "Café".
        normalized_q = func.dish_name_normalized(cleaned_q)
        stmt = stmt.where(
            or_(
                Dish.name.ilike(f"%{cleaned_q}%"),
                Dish.name_normalized.like(func.concat("%", normalized_q, "%")),
            )
        )
    stmt = stmt.order_by(Dish.review_count.desc(), Dish.name.asc()).limit(limit)

    rows = (await db.execute(stmt)).scalars().all()
    return DishSearchPage(
        items=[DishSearchItem(id=d.id, name=d.name) for d in rows]
    )


# similarity threshold below which we don't bother showing a suggestion —
# pg_trgm typically lands at ~0.3 for the SET pg_trgm.similarity_threshold default;
# we go a touch higher because false-positive "did you mean..." prompts annoy users.
_SIMILARITY_THRESHOLD = 0.4


@router.get("/api/dishes/suggest-similar", response_model=DishSuggestionPage)
async def suggest_similar_dishes(
    db: Annotated[AsyncSession, Depends(get_db)],
    restaurant_place_id: str = Query(min_length=1, max_length=200),
    name: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=5, ge=1, le=10),
) -> DishSuggestionPage:
    """
    Before creating a brand-new dish from compose, the frontend asks here:
    "is the user about to duplicate something?". We answer with up to `limit`
    candidates from the same restaurant whose normalized name is either an
    exact match (the duplicate) or trigram-similar above the threshold.

    Empty list = user input is novel; create freely.
    """
    cleaned = name.strip()
    if not cleaned:
        return DishSuggestionPage(items=[])

    restaurant = (
        await db.execute(
            select(Restaurant).where(Restaurant.google_place_id == restaurant_place_id)
        )
    ).scalar_one_or_none()
    if restaurant is None:
        return DishSuggestionPage(items=[])

    normalized_input = func.dish_name_normalized(cleaned)
    similarity = func.similarity(Dish.name_normalized, normalized_input)

    stmt = (
        select(
            Dish.id,
            Dish.name,
            Dish.review_count,
            Dish.name_normalized,
            similarity.label("sim"),
        )
        .where(
            Dish.restaurant_id == restaurant.id,
            similarity >= _SIMILARITY_THRESHOLD,
        )
        .order_by(similarity.desc(), Dish.review_count.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()

    # Compute the exact normalized form once on the DB side too — easier than
    # mirroring the SQL function in Python (and guaranteed to agree with the
    # unique index).
    exact_normalized = (
        await db.execute(select(normalized_input))
    ).scalar_one()

    return DishSuggestionPage(
        items=[
            DishSuggestion(
                id=row.id,
                name=row.name,
                review_count=row.review_count,
                similarity=float(row.sim),
                is_exact_normalized=(row.name_normalized == exact_normalized),
            )
            for row in rows
        ]
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


@router.post(
    "/api/dishes/{source_id}/merge",
    response_model=DishMergeResponse,
)
async def merge_dish(
    source_id: uuid.UUID,
    payload: DishMergeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(require_role(UserRole.admin))],
) -> dict:
    """Admin: merge `source_id` dish into `payload.target_id`.

    Both dishes must live in the same restaurant. Moves all reviews from
    source to target, optionally inherits the cover image when target has
    none, deletes the source row + its dish_cover images, and recomputes
    the target's rating aggregate. Whole operation is one transaction.
    """
    try:
        summary = await merge_dishes(
            db,
            source_id=source_id,
            target_id=payload.target_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return summary
