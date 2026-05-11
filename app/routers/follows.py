import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, get_current_user_optional
from app.middleware.rate_limit import FOLLOW_LIMIT, limiter
from app.models.follow import Follow
from app.models.user import User
from app.schemas.social import (
    FollowActionResponse,
    FollowerSummary,
    FollowersPage,
    UserSuggestion,
    UserSuggestionsPage,
)
from app.services.notification_service import record_follow_notification
from app.services.safety_service import is_blocked_either_way

router = APIRouter(prefix="/api/users", tags=["follows"])


async def _resolve_user(db: AsyncSession, id_or_handle: str) -> User:
    """Find a user by UUID or by lowercase handle. 404 otherwise."""
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
    return user


async def _followers_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Follow)
        .where(Follow.following_id == user_id)
    )
    return int(result.scalar_one() or 0)


@router.post("/{id_or_handle}/follow", response_model=FollowActionResponse)
@limiter.limit(FOLLOW_LIMIT)
async def follow_user(
    request: Request,
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FollowActionResponse:
    """Idempotent: following an already-followed user returns the same result."""
    target = await _resolve_user(db, id_or_handle)
    if target.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No podés seguirte a vos mismo.",
        )

    # Block bidireccional: ningún lado puede iniciar follow tras un block.
    # 404 (no 403) para no filtrar quién bloqueó a quién.
    if await is_blocked_either_way(db, current_user.id, target.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )

    existing = await db.execute(
        select(Follow).where(
            Follow.follower_id == current_user.id,
            Follow.following_id == target.id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(Follow(follower_id=current_user.id, following_id=target.id))
        await record_follow_notification(
            db, actor_id=current_user.id, target_user_id=target.id
        )
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent insert race — the other insert won, we're already
            # following; proceed as if the call succeeded.
            await db.rollback()

    followers = await _followers_count(db, target.id)
    return FollowActionResponse(
        follower_id=current_user.id,
        following_id=target.id,
        following=True,
        followers_count=followers,
    )


@router.delete("/{id_or_handle}/follow", response_model=FollowActionResponse)
@limiter.limit(FOLLOW_LIMIT)
async def unfollow_user(
    request: Request,
    id_or_handle: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FollowActionResponse:
    """Idempotent: unfollowing a user you don't follow returns the same result."""
    target = await _resolve_user(db, id_or_handle)

    existing = await db.execute(
        select(Follow).where(
            Follow.follower_id == current_user.id,
            Follow.following_id == target.id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.flush()

    followers = await _followers_count(db, target.id)
    return FollowActionResponse(
        follower_id=current_user.id,
        following_id=target.id,
        following=False,
        followers_count=followers,
    )


@router.get("/{id_or_handle}/followers", response_model=FollowersPage)
async def list_followers(
    id_or_handle: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)] = None,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> FollowersPage:
    target = await _resolve_user(db, id_or_handle)
    return await _list_follow_edges(
        db,
        side="followers",
        user_id=target.id,
        cursor=cursor,
        limit=limit,
        viewer=viewer,
    )


@router.get("/{id_or_handle}/following", response_model=FollowersPage)
async def list_following(
    id_or_handle: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    viewer: Annotated[User | None, Depends(get_current_user_optional)] = None,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> FollowersPage:
    target = await _resolve_user(db, id_or_handle)
    return await _list_follow_edges(
        db,
        side="following",
        user_id=target.id,
        cursor=cursor,
        limit=limit,
        viewer=viewer,
    )


@router.get("/me/suggestions", response_model=UserSuggestionsPage)
async def suggest_users_to_follow(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=10, ge=1, le=50),
) -> UserSuggestionsPage:
    """People-you-may-know.

    Combina dos señales en SQL puro (raw text para que la CTE sea legible):

    - **Friends-of-friends**: gente que sigue >=1 de los que el viewer ya
      sigue. ``shared_followers`` = cuántos del grafo del viewer la
      siguen.
    - **Co-reviewers**: gente que reseñó >=1 restaurante donde el viewer
      también reseñó. ``shared_restaurants`` = cantidad de restaurantes
      en común.

    Score: ``shared_followers * 3 + shared_restaurants``. Excluye:
    - el viewer mismo
    - usuarios que ya sigue
    - usuarios bloqueados (cualquier dirección) o muteados por el viewer.

    Habilitado por ``ix_follows_following_id`` de la migración 056. El
    audit dejó pendiente un re-rank por cosine de embeddings sobre el
    top-50 (señal de "gusto gastronómico"). No se implementó en v1:
    requiere materializar centroides por usuario para no degradar. El
    score actual con 2 señales suele bastar; el cosine se agrega si la
    métrica de conversión (follow desde sugerencia) sale baja.
    """
    sql = text(
        """
        WITH excluded AS (
            SELECT blocked_id AS uid FROM user_blocks WHERE blocker_id = :viewer_id
            UNION ALL
            SELECT blocker_id FROM user_blocks WHERE blocked_id = :viewer_id
            UNION ALL
            SELECT muted_id FROM user_mutes WHERE muter_id = :viewer_id
            UNION ALL
            SELECT following_id FROM follows WHERE follower_id = :viewer_id
            UNION ALL
            SELECT CAST(:viewer_id AS uuid)
        ),
        fof AS (
            SELECT f2.following_id AS candidate_id,
                   COUNT(DISTINCT f1.following_id) AS shared_followers
            FROM follows f1
            JOIN follows f2 ON f2.follower_id = f1.following_id
            WHERE f1.follower_id = :viewer_id
              AND f2.following_id NOT IN (SELECT uid FROM excluded)
            GROUP BY f2.following_id
        ),
        co_reviewers AS (
            SELECT dr2.user_id AS candidate_id,
                   COUNT(DISTINCT d1.restaurant_id) AS shared_restaurants
            FROM dish_reviews dr1
            JOIN dishes d1 ON d1.id = dr1.dish_id
            JOIN dishes d2 ON d2.restaurant_id = d1.restaurant_id
            JOIN dish_reviews dr2 ON dr2.dish_id = d2.id
            WHERE dr1.user_id = :viewer_id
              AND dr2.user_id <> :viewer_id
              AND dr2.user_id NOT IN (SELECT uid FROM excluded)
            GROUP BY dr2.user_id
        ),
        candidates AS (
            SELECT
                COALESCE(fof.candidate_id, co_reviewers.candidate_id) AS candidate_id,
                COALESCE(fof.shared_followers, 0) AS shared_followers,
                COALESCE(co_reviewers.shared_restaurants, 0) AS shared_restaurants
            FROM fof
            FULL OUTER JOIN co_reviewers
                ON fof.candidate_id = co_reviewers.candidate_id
        )
        SELECT
            u.id,
            u.display_name,
            u.handle,
            u.avatar_url,
            u.bio,
            c.shared_followers,
            c.shared_restaurants
        FROM candidates c
        JOIN users u ON u.id = c.candidate_id
        ORDER BY (c.shared_followers * 3 + c.shared_restaurants) DESC,
                 u.id
        LIMIT :limit
        """
    )
    result = await db.execute(
        sql, {"viewer_id": str(current_user.id), "limit": limit}
    )
    rows = result.mappings().all()
    items = [
        UserSuggestion(
            id=row["id"],
            display_name=row["display_name"],
            handle=row["handle"],
            avatar_url=row["avatar_url"],
            bio=row["bio"],
            shared_followers=int(row["shared_followers"] or 0),
            shared_restaurants=int(row["shared_restaurants"] or 0),
        )
        for row in rows
    ]
    return UserSuggestionsPage(items=items)


async def _list_follow_edges(
    db: AsyncSession,
    *,
    side: str,  # "followers" | "following"
    user_id: uuid.UUID,
    cursor: str | None,
    limit: int,
    viewer: User | None,
) -> FollowersPage:
    cursor_dt: datetime | None = None
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cursor inválido",
            )

    if side == "followers":
        # The followers of `user_id` → join Follow.follower_id → User.id.
        stmt = (
            select(User, Follow.created_at)
            .join(Follow, Follow.follower_id == User.id)
            .where(Follow.following_id == user_id)
        )
    else:
        stmt = (
            select(User, Follow.created_at)
            .join(Follow, Follow.following_id == User.id)
            .where(Follow.follower_id == user_id)
        )

    stmt = stmt.order_by(Follow.created_at.desc()).limit(limit + 1)
    if cursor_dt is not None:
        stmt = stmt.where(Follow.created_at < cursor_dt)

    rows = (await db.execute(stmt)).all()
    has_more = len(rows) > limit
    trimmed = rows[:limit]

    # Bulk-resolve viewer_following for the page in a single query, using the
    # PK index on follows (follower_id, following_id). At most `limit` IDs
    # (cap 100), so the IN clause is bounded.
    viewer_follows: set[uuid.UUID] = set()
    if viewer is not None and trimmed:
        item_ids = [user.id for user, _ in trimmed]
        res = await db.execute(
            select(Follow.following_id).where(
                Follow.follower_id == viewer.id,
                Follow.following_id.in_(item_ids),
            )
        )
        viewer_follows = {row[0] for row in res.all()}

    items = [
        FollowerSummary(
            id=user.id,
            display_name=user.display_name,
            handle=user.handle,
            avatar_url=user.avatar_url,
            bio=user.bio,
            created_at=created_at,
            viewer_following=(user.id in viewer_follows) if viewer is not None else None,
        )
        for user, created_at in trimmed
    ]
    next_cursor = trimmed[-1][1].isoformat() if has_more and trimmed else None
    return FollowersPage(items=items, next_cursor=next_cursor)
