"""In-process notification writer.

Each social action (like, comment, follow) inserts one row in `notifications`
for the recipient; self-actions are skipped. The caller is responsible for
commit semantics — this module only stages `db.add(...)`.

Text is denormalized at insert time so the inbox renders without joins.
"""

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import Dish, DishReview
from app.models.social import Notification


@dataclass(frozen=True)
class _ReviewLikeContext:
    dish_name: Optional[str]
    presentation: Optional[int]
    value_prop: Optional[int]
    execution: Optional[int]


# Tiebreaker para el "pilar destacado" cuando la review tiene varios pilares
# en 3 (Perfección/Ganga/Increíble). Sigue los pesos del Geek Score:
# execution (45%) > value_prop (25%) > presentation (15%).
_PILLAR_PRIORITY: tuple[str, ...] = ("execution", "value_prop", "presentation")

# Texto enriquecido por pilar. Se usa cuando el pilar correspondiente vale 3.
_PILLAR_TEXT: dict[str, tuple[str, str]] = {
    "execution": ("Ejecución", "👨‍🍳"),
    "value_prop": ("hallazgo", "💎"),
    "presentation": ("Presentación", "🌟"),
}


async def _review_like_context(
    db: AsyncSession, review_id: uuid.UUID
) -> _ReviewLikeContext:
    """Lookup the dish name + technical pillars for a review in one query."""
    result = await db.execute(
        select(
            Dish.name,
            DishReview.presentation,
            DishReview.value_prop,
            DishReview.execution,
        )
        .select_from(DishReview)
        .join(Dish, Dish.id == DishReview.dish_id)
        .where(DishReview.id == review_id)
    )
    row = result.one_or_none()
    if row is None:
        return _ReviewLikeContext(None, None, None, None)
    return _ReviewLikeContext(
        dish_name=row[0],
        presentation=row[1],
        value_prop=row[2],
        execution=row[3],
    )


def _top_pillar(ctx: _ReviewLikeContext) -> Optional[str]:
    """Pilar con valor 3 más relevante según `_PILLAR_PRIORITY`."""
    pillar_values: dict[str, Optional[int]] = {
        "execution": ctx.execution,
        "value_prop": ctx.value_prop,
        "presentation": ctx.presentation,
    }
    for key in _PILLAR_PRIORITY:
        if pillar_values[key] == 3:
            return key
    return None


def _build_like_text(ctx: _ReviewLikeContext) -> str:
    pillar = _top_pillar(ctx)
    if pillar is not None and ctx.dish_name is not None:
        label, emoji = _PILLAR_TEXT[pillar]
        return f"le encantó tu {label} {emoji} en tu reseña de {ctx.dish_name}."
    if ctx.dish_name is not None:
        return f"le dio like a tu reseña de {ctx.dish_name}."
    return "le dio like a tu reseña."


async def record_like_notification(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    review_id: uuid.UUID,
    review_owner_id: uuid.UUID,
) -> None:
    if actor_id == review_owner_id:
        return
    ctx = await _review_like_context(db, review_id)
    db.add(
        Notification(
            recipient_user_id=review_owner_id,
            actor_user_id=actor_id,
            kind="like",
            target_review_id=review_id,
            text=_build_like_text(ctx),
        )
    )


async def record_comment_notification(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    review_id: uuid.UUID,
    review_owner_id: uuid.UUID,
    comment_body: str,
) -> None:
    if actor_id == review_owner_id:
        return
    excerpt = (comment_body[:60] + "…") if len(comment_body) > 60 else comment_body
    text = f'comentó tu reseña: "{excerpt}"'
    db.add(
        Notification(
            recipient_user_id=review_owner_id,
            actor_user_id=actor_id,
            kind="comment",
            target_review_id=review_id,
            text=text,
        )
    )


async def record_follow_notification(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> None:
    if actor_id == target_user_id:
        return
    db.add(
        Notification(
            recipient_user_id=target_user_id,
            actor_user_id=actor_id,
            kind="follow",
            target_user_id=actor_id,
            text="empezó a seguirte.",
        )
    )
