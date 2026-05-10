"""Block / mute primitives — single source of truth for safety checks.

Cuatro callers usan estas funciones; centralizarlas evita drift entre
qué considera "bloqueo bidireccional" cada path:

- ``routers/safety.py`` (block/unblock + mute/unmute endpoints).
- ``routers/follows.py`` (follow rechazado si hay bloqueo en cualquier
  dirección).
- ``services/notification_service.py`` (no notificar al recipient si
  hay bloqueo en cualquier dirección o si el recipient muteó al actor).
- ``routers/feed.py`` (excluir reviews de autores bloqueados/muteados).

Convenciones:

- "block bidireccional" = ``A blocked B`` OR ``B blocked A``. Sin
  importar quién bloqueó primero, ninguna acción social cruza la
  frontera.
- "mute" = unidireccional. ``muter_id`` deja de ver al ``muted_id``;
  el muted no se entera. NO afecta lo que el muted ve del muter.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.social import UserBlock, UserMute


async def is_blocked_either_way(
    db: AsyncSession, user_a: uuid.UUID, user_b: uuid.UUID
) -> bool:
    """True if A blocked B or B blocked A."""
    if user_a == user_b:
        return False
    result = await db.execute(
        select(UserBlock.blocker_id).where(
            or_(
                (UserBlock.blocker_id == user_a) & (UserBlock.blocked_id == user_b),
                (UserBlock.blocker_id == user_b) & (UserBlock.blocked_id == user_a),
            )
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def is_muted_by(
    db: AsyncSession, muter_id: uuid.UUID, muted_id: uuid.UUID
) -> bool:
    """True if ``muter_id`` silenced ``muted_id``."""
    if muter_id == muted_id:
        return False
    result = await db.execute(
        select(UserMute.muter_id).where(
            UserMute.muter_id == muter_id,
            UserMute.muted_id == muted_id,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def should_deliver_notification(
    db: AsyncSession,
    *,
    recipient_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> bool:
    """Notification gate: True si la notif puede entregarse.

    Bloquea cuando:
    - hay block en cualquier dirección entre actor y recipient
    - el recipient muteó al actor
    """
    if recipient_id == actor_id:
        # caller already skips self-actions, defensa en profundidad
        return False
    if await is_blocked_either_way(db, recipient_id, actor_id):
        return False
    if await is_muted_by(db, recipient_id, actor_id):
        return False
    return True


def excluded_author_ids_subquery(viewer_id: uuid.UUID):
    """Subquery con ``user_id`` que el viewer no debe ver en feeds.

    Combina:
    - usuarios que el viewer bloqueó
    - usuarios que bloquearon al viewer
    - usuarios que el viewer muteó

    Usar en ``feed.py`` como ``DishReview.user_id.notin_(subquery)``.
    Devuelve un ``Select`` que SQLAlchemy embebe inline; no toca la DB
    hasta que se ejecuta el statement contenedor.
    """
    blocked_by_viewer = select(UserBlock.blocked_id.label("uid")).where(
        UserBlock.blocker_id == viewer_id
    )
    blockers_of_viewer = select(UserBlock.blocker_id.label("uid")).where(
        UserBlock.blocked_id == viewer_id
    )
    muted_by_viewer = select(UserMute.muted_id.label("uid")).where(
        UserMute.muter_id == viewer_id
    )
    # ``union_all``: el ``IN`` filtra por set-membership, así que duplicados
    # entre los 3 grupos no cambian el resultado y nos ahorramos el DISTINCT.
    return union_all(blocked_by_viewer, blockers_of_viewer, muted_by_viewer)
