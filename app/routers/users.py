import math
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.db_errors import is_unique_violation
from app.middleware.auth import get_current_user, get_current_user_optional
from app.models.category import Category
from app.models.dish import Dish, DishReview
from app.models.follow import Follow
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.feed import FeedPage
from app.schemas.user import (
    CategoryStat,
    FeaturedTitle,
    MasteryLevel,
    PublicUserResponse,
    PublicViewerState,
    UserCounts,
    UserProfileUpdate,
    UserReputation,
    UserResponse,
)

router = APIRouter(prefix="/api/users", tags=["users"])


@router.patch("/me", response_model=UserResponse)
async def update_my_profile(
    payload: UserProfileUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Partial update of the authenticated user's own profile fields."""
    data = payload.model_dump(exclude_unset=True)

    if "handle" in data and data["handle"] is not None:
        data["handle"] = data["handle"].lower()

    for field, value in data.items():
        setattr(current_user, field, value)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if is_unique_violation(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ese handle ya está en uso.",
            )
        raise

    await db.refresh(current_user)
    return current_user


@router.get("/{id_or_handle}", response_model=PublicUserResponse)
async def get_public_profile(
    id_or_handle: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
) -> PublicUserResponse:
    """Public profile lookup by UUID or handle. Handles are case-insensitive."""
    user: User | None = None

    try:
        user_uuid = uuid.UUID(id_or_handle)
        result = await db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
    except ValueError:
        handle = id_or_handle.lower().strip()
        if handle:
            result = await db.execute(select(User).where(User.handle == handle))
            user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )

    # Aggregates.
    review_count = (
        await db.execute(
            select(func.count())
            .select_from(DishReview)
            .where(DishReview.user_id == user.id)
        )
    ).scalar_one() or 0

    followers = (
        await db.execute(
            select(func.count())
            .select_from(Follow)
            .where(Follow.following_id == user.id)
        )
    ).scalar_one() or 0

    following = (
        await db.execute(
            select(func.count())
            .select_from(Follow)
            .where(Follow.follower_id == user.id)
        )
    ).scalar_one() or 0

    reputation = await _build_reputation(db, user.id)

    # Viewer context (anonymous → all false).
    is_self = viewer is not None and viewer.id == user.id
    viewer_following = False
    if viewer is not None and not is_self:
        existing = await db.execute(
            select(Follow).where(
                Follow.follower_id == viewer.id,
                Follow.following_id == user.id,
            )
        )
        viewer_following = existing.scalar_one_or_none() is not None

    return PublicUserResponse(
        id=user.id,
        display_name=user.display_name,
        handle=user.handle,
        avatar_url=user.avatar_url,
        bio=user.bio,
        location=user.location,
        counts=UserCounts(
            reviews=int(review_count),
            followers=int(followers),
            following=int(following),
        ),
        reputation=reputation,
        viewer_state=PublicViewerState(
            is_self=is_self,
            following=viewer_following,
        ),
    )


# Mínimo de reviews por categoría para que califique como "especialidad" del
# usuario. Por debajo de eso, una categoría puede aparecer por casualidad y
# no representa criterio. 2 es razonable para dev / early stage; subir a 3+
# cuando haya más data.
_MIN_REVIEWS_PER_CATEGORY = 2
_TOP_CATEGORIES_LIMIT = 3

# Umbrales escalonados de "maestría" en una categoría. El gastronerd colecciona
# títulos: apprentice → sommelier → master. None cuando no llega al primero.
# Los pisos de avg_rating evitan que el volumen solo otorgue título.
_MASTERY_TIERS: tuple[tuple[MasteryLevel, int, float], ...] = (
    ("master", 25, 4.0),
    ("sommelier", 10, 3.8),
    ("apprentice", 3, 3.5),
)
_LEVEL_RANK: dict[MasteryLevel, int] = {"apprentice": 1, "sommelier": 2, "master": 3}


def _classify_mastery(review_count: int, avg_rating: float) -> MasteryLevel | None:
    for level, min_count, min_avg in _MASTERY_TIERS:
        if review_count >= min_count and avg_rating >= min_avg:
            return level
    return None


async def _build_reputation(
    db: AsyncSession, user_id: uuid.UUID
) -> UserReputation:
    """Calcula reviews verificadas, restos visitados y top categorías."""
    # Reviews verificadas: 3 pilares NOT NULL.
    verified = (
        await db.execute(
            select(func.count())
            .select_from(DishReview)
            .where(
                DishReview.user_id == user_id,
                DishReview.presentation.is_not(None),
                DishReview.value_prop.is_not(None),
                DishReview.execution.is_not(None),
            )
        )
    ).scalar_one() or 0

    # Restaurantes únicos reseñados.
    visited = (
        await db.execute(
            select(func.count(func.distinct(Dish.restaurant_id)))
            .select_from(DishReview)
            .join(Dish, Dish.id == DishReview.dish_id)
            .where(DishReview.user_id == user_id)
        )
    ).scalar_one() or 0

    # Top categorías: agregamos por categoría del restaurante, filtramos por
    # volumen mínimo y rankeamos por (avg_rating × log(1 + count)) — combina
    # consistencia (volumen) con valoración alta. Tope arbitrario de 3.
    rows = (
        await db.execute(
            select(
                Category.name.label("name"),
                func.count(DishReview.id).label("review_count"),
                func.avg(DishReview.rating).label("avg_rating"),
            )
            .select_from(DishReview)
            .join(Dish, Dish.id == DishReview.dish_id)
            .join(Restaurant, Restaurant.id == Dish.restaurant_id)
            .join(Category, Category.id == Restaurant.category_id)
            .where(DishReview.user_id == user_id)
            .group_by(Category.name)
            .having(func.count(DishReview.id) >= _MIN_REVIEWS_PER_CATEGORY)
        )
    ).all()

    scored: list[CategoryStat] = []
    for r in rows:
        count = int(r.review_count or 0)
        avg = float(r.avg_rating or 0.0)
        score = avg * math.log1p(count)
        scored.append(
            CategoryStat(
                name=r.name,
                review_count=count,
                avg_rating=round(avg, 2),
                score=round(score, 4),
                mastery_level=_classify_mastery(count, avg),
            )
        )
    scored.sort(key=lambda c: c.score, reverse=True)

    # Featured title: el de mayor nivel; tie-break por review_count.
    titled = [c for c in scored if c.mastery_level is not None]
    titled.sort(
        key=lambda c: (_LEVEL_RANK[c.mastery_level], c.review_count),  # type: ignore[index]
        reverse=True,
    )
    featured = (
        FeaturedTitle(category=titled[0].name, level=titled[0].mastery_level)  # type: ignore[arg-type]
        if titled
        else None
    )

    return UserReputation(
        verified_review_count=int(verified),
        restaurants_visited=int(visited),
        top_categories=scored[:_TOP_CATEGORIES_LIMIT],
        featured_title=featured,
    )


@router.get("/{id_or_handle}/reviews", response_model=FeedPage)
async def get_user_reviews(
    id_or_handle: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
) -> FeedPage:
    """Public timeline of reviews authored by a user. Accepts UUID or handle.

    The string ``me`` is reserved by the legacy `/api/users/me/reviews`
    endpoint and intentionally 404'd here so callers don't silently shadow
    the legacy response shape.
    """
    # Lazy import to avoid a top-level circular (`feed.py` imports users
    # transitively only through model references).
    from app.routers.feed import _build_feed_items

    if id_or_handle == "me":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usá /api/users/{id o handle} con tu propio id.",
        )

    # Resolve by UUID, then by handle.
    user: User | None = None
    try:
        user_uuid = uuid.UUID(id_or_handle)
        result = await db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
    except ValueError:
        handle = id_or_handle.lower().strip()
        if handle:
            result = await db.execute(select(User).where(User.handle == handle))
            user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )

    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Cursor inválido")

    items, has_more = await _build_feed_items(
        db,
        viewer,
        base_filters=[DishReview.user_id == user.id],
        cursor_dt=cursor_dt,
        limit=limit,
    )
    next_cursor = items[-1].created_at.isoformat() if has_more and items else None
    return FeedPage(items=items, next_cursor=next_cursor)
