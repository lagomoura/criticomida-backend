import time
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


# --- People-you-may-know: caché TTL en proceso ---------------------------
#
# Las sugerencias no cambian segundo a segundo y la query es cara (FoF +
# co-reviewers, y para low-signal además un agregado sobre ``follows``).
# Sin Redis en el stack, un TTL hand-rolled por proceso ya recorta la
# carga: el rail "Para vos" se monta en cada visita al feed. Stale de
# ~10 min es aceptable para sugerencias. Si el proceso se recicla o
# escala horizontal, cada worker calienta su propia copia: aceptable.
_SUGGESTION_CACHE: dict[tuple[str, int], tuple[float, UserSuggestionsPage]] = {}
_SUGGESTION_CACHE_TTL = 600.0
_SUGGESTION_CACHE_MAX = 5000

# CTE de exclusión compartida por la query de señal y la de fallback:
# nunca sugerir bloqueados (cualquier dirección), muteados, ya seguidos
# ni al propio viewer.
_EXCLUDED_CTE = """
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
        )"""


def _suggestion_cache_get(key: tuple[str, int]) -> UserSuggestionsPage | None:
    hit = _SUGGESTION_CACHE.get(key)
    if hit is None:
        return None
    expires_at, page = hit
    if expires_at < time.monotonic():
        _SUGGESTION_CACHE.pop(key, None)
        return None
    return page


def _suggestion_cache_put(key: tuple[str, int], page: UserSuggestionsPage) -> None:
    # Evicción cruda: al llegar al tope se vacía todo. Es infrecuente
    # (5000 viewers distintos por proceso dentro de la ventana de TTL) y
    # evita arrastrar una LRU para un path no crítico.
    if len(_SUGGESTION_CACHE) >= _SUGGESTION_CACHE_MAX:
        _SUGGESTION_CACHE.clear()
    _SUGGESTION_CACHE[key] = (time.monotonic() + _SUGGESTION_CACHE_TTL, page)


def _suggestion_cache_invalidate(viewer_id: str) -> None:
    """Tira la cache de sugerencias de un viewer (todos los ``limit``).

    Se llama tras follow/unfollow: el set de exclusión
    (``_EXCLUDED_CTE``) cambió, así que la lista cacheada quedó stale —
    seguiría mostrando a alguien que el viewer ya sigue. Sin esto el
    rail "Para vos" parece *no persistir* el follow: el usuario sigue
    apareciendo con botón "Seguir" hasta que vence el TTL.
    """
    stale = [k for k in _SUGGESTION_CACHE if k[0] == viewer_id]
    for k in stale:
        _SUGGESTION_CACHE.pop(k, None)


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

    _suggestion_cache_invalidate(str(current_user.id))
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

    _suggestion_cache_invalidate(str(current_user.id))
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

    **Señal** (SQL puro, raw text para que las CTE sean legibles):

    - **Friends-of-friends**: gente que sigue >=1 de los que el viewer ya
      sigue. ``shared_followers`` = cuántos del grafo del viewer la
      siguen.
    - **Co-reviewers**: gente que reseñó >=1 restaurante donde el viewer
      también reseñó. ``shared_restaurants`` = cantidad de restaurantes
      en común.

    Score: ``shared_followers * 3 + shared_restaurants``. ``reason_kind``
    = ``signal`` en estos. Acotado para que no explote en restaurantes
    muy reseñados: el set de restaurantes del viewer se topea (CTE
    ``viewer_restaurants LIMIT 100``) y los co-reviewers se truncan a los
    top 200 por restaurantes en común.

    **Cold-start**: si la señal no llena ``limit`` (usuario nuevo sin
    grafo ni reseñas), se rellena con un fallback de críticos primero y
    luego por popularidad general (``reason_kind`` = ``popular_critic`` /
    ``popular``). Así el rail nunca queda vacío para quien más necesita
    descubrir gente. El fallback solo corre cuando hace falta, así que
    los usuarios activos (la mayoría) no pagan su costo.

    Exclusiones (compartidas por señal y fallback vía ``_EXCLUDED_CTE``):
    el viewer mismo, ya seguidos, bloqueados (cualquier dirección) o
    muteados.

    Resultado cacheado por ``(viewer, limit)`` ~10 min (ver
    ``_SUGGESTION_CACHE``). Habilitado por ``ix_follows_following_id``
    (migración 056). Pendiente de audit: re-rank por cosine de embeddings
    sobre el top-50; no en v1 (requiere materializar centroides).
    """
    cache_key = (str(current_user.id), limit)
    cached = _suggestion_cache_get(cache_key)
    if cached is not None:
        return cached

    viewer_id = str(current_user.id)

    signal_sql = text(
        _EXCLUDED_CTE
        + """,
        viewer_restaurants AS (
            SELECT DISTINCT d.restaurant_id
            FROM dish_reviews dr
            JOIN dishes d ON d.id = dr.dish_id
            WHERE dr.user_id = :viewer_id
            LIMIT 100
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
                   COUNT(DISTINCT d2.restaurant_id) AS shared_restaurants
            FROM viewer_restaurants vr
            JOIN dishes d2 ON d2.restaurant_id = vr.restaurant_id
            JOIN dish_reviews dr2 ON dr2.dish_id = d2.id
            WHERE dr2.user_id <> :viewer_id
              AND dr2.user_id NOT IN (SELECT uid FROM excluded)
            GROUP BY dr2.user_id
            ORDER BY shared_restaurants DESC
            LIMIT 200
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
        signal_sql, {"viewer_id": viewer_id, "limit": limit}
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
            reason_kind="signal",
        )
        for row in rows
    ]

    # Cold-start: rellenar los slots vacíos con un fallback. Solo se
    # ejecuta cuando la señal no alcanzó (usuario nuevo / red chica), así
    # que el agregado sobre ``follows`` no lo paga el usuario activo.
    if len(items) < limit:
        need = limit - len(items)
        picked_ids = [str(it.id) for it in items]
        params: dict[str, object] = {"viewer_id": viewer_id, "need": need}
        exclude_clause = ""
        if picked_ids:
            placeholders = ",".join(f":pk{i}" for i in range(len(picked_ids)))
            exclude_clause = f"AND u.id NOT IN ({placeholders})"
            for i, pid in enumerate(picked_ids):
                params[f"pk{i}"] = pid

        fallback_sql = text(
            _EXCLUDED_CTE
            + f""",
        follower_counts AS (
            SELECT following_id AS uid, COUNT(*) AS n
            FROM follows
            GROUP BY following_id
        )
        SELECT
            u.id,
            u.display_name,
            u.handle,
            u.avatar_url,
            u.bio,
            (u.role = 'critic') AS is_critic
        FROM users u
        LEFT JOIN follower_counts fc ON fc.uid = u.id
        WHERE u.id NOT IN (SELECT uid FROM excluded)
          {exclude_clause}
        ORDER BY (u.role = 'critic') DESC,
                 COALESCE(fc.n, 0) DESC,
                 u.id
        LIMIT :need
        """
        )
        fb_result = await db.execute(fallback_sql, params)
        for row in fb_result.mappings().all():
            items.append(
                UserSuggestion(
                    id=row["id"],
                    display_name=row["display_name"],
                    handle=row["handle"],
                    avatar_url=row["avatar_url"],
                    bio=row["bio"],
                    shared_followers=0,
                    shared_restaurants=0,
                    reason_kind=(
                        "popular_critic" if row["is_critic"] else "popular"
                    ),
                )
            )

    page = UserSuggestionsPage(items=items)
    _suggestion_cache_put(cache_key, page)
    return page


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
