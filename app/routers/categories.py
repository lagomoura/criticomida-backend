from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import require_role
from app.models.category import Category
from app.models.dish import Dish
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.schemas.category import (
    CategoryCreate,
    CategoryPendingResponse,
    CategoryRejectRequest,
    CategoryResponse,
    CategoryUpdate,
)

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
    # Filtro público: las categorías pendientes de revisión no aparecen
    # en el index ni en ningún surface público hasta que el admin las
    # apruebe desde /admin/categorias-pendientes. Las pendientes siguen
    # siendo `category_id` válido para restaurants y se siguen contando
    # internamente, pero no son seleccionables ni filtrables.
    result = await db.execute(
        select(Category, review_count_subq.label("review_count"))
        .where(Category.pending_review.is_(False))
        .order_by(Category.display_order, Category.name)
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
            "parent_id": cat.parent_id,
            "review_count": count,
        }
        for cat, count in rows
    ]


@router.get("/pending", response_model=list[CategoryPendingResponse])
async def list_pending_categories(
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
) -> list[dict]:
    """Cola de categorías auto-creadas por el servicio de inferencia.

    Solo admins. Cada fila incluye el conteo de restaurantes que ya
    quedaron apuntando para que la decisión 'approve vs reject vs merge'
    sea consciente del impacto.
    """
    restaurant_count_subq = (
        select(func.count())
        .where(Restaurant.category_id == Category.id)
        .correlate(Category)
        .scalar_subquery()
    )
    result = await db.execute(
        select(Category, restaurant_count_subq.label("restaurant_count"))
        .where(Category.pending_review.is_(True))
        .order_by(Category.id.desc())
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
            "parent_id": cat.parent_id,
            "restaurant_count": int(count),
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
        "parent_id": category.parent_id,
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


@router.post(
    "/{slug}/approve",
    response_model=CategoryResponse,
    status_code=status.HTTP_200_OK,
)
async def approve_pending_category(
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
) -> Category:
    """Marca una categoría pendiente como aprobada (la expone al público).

    Idempotente: aprobar una ya aprobada es 200 OK sin cambios. Cualquier
    admin puede hacerlo desde la cola.
    """
    result = await db.execute(select(Category).where(Category.slug == slug))
    category = result.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )
    if category.pending_review:
        category.pending_review = False
        await db.flush()
        await db.refresh(category)
    return category


@router.post(
    "/{slug}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reject_pending_category(
    slug: str,
    payload: CategoryRejectRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _admin: Annotated[User, Depends(require_role(UserRole.admin))],
) -> None:
    """Rechaza una pendiente: re-asigna sus restaurantes a `target_slug`
    (default `otros`) y borra la fila. Solo permite rechazar pendientes
    para no abrir un path de borrado de categorías canónicas vía este
    endpoint — el DELETE clásico sigue habilitado para eso.
    """
    cat_q = await db.execute(select(Category).where(Category.slug == slug))
    category = cat_q.scalar_one_or_none()
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )
    if not category.pending_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending categories can be rejected via this endpoint",
        )

    target_q = await db.execute(
        select(Category).where(Category.slug == payload.target_slug)
    )
    target = target_q.scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Target category '{payload.target_slug}' not found",
        )
    if target.id == category.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_slug cannot be the rejected category itself",
        )
    if target.pending_review:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reassign to another pending category",
        )

    # Re-apuntar restaurants antes de borrar.
    await db.execute(
        Restaurant.__table__.update()
        .where(Restaurant.category_id == category.id)
        .values(category_id=target.id)
    )
    await db.delete(category)
    await db.flush()


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
