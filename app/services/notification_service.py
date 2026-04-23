"""In-process notification writer.

Each social action (like, comment, follow) inserts one row in `notifications`
for the recipient; self-actions are skipped. The caller is responsible for
commit semantics — this module only stages `db.add(...)`.

Text is denormalized at insert time so the inbox renders without joins.
"""

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import Dish, DishReview
from app.models.social import Notification


async def _dish_name_for_review(
    db: AsyncSession, review_id: uuid.UUID
) -> Optional[str]:
    """Best-effort lookup of the dish name tied to a review."""
    result = await db.execute(
        select(Dish.name)
        .select_from(DishReview)
        .join(Dish, Dish.id == DishReview.dish_id)
        .where(DishReview.id == review_id)
    )
    return result.scalar_one_or_none()


async def record_like_notification(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    review_id: uuid.UUID,
    review_owner_id: uuid.UUID,
) -> None:
    if actor_id == review_owner_id:
        return
    dish_name = await _dish_name_for_review(db, review_id)
    text = (
        f"le dio like a tu reseña de {dish_name}."
        if dish_name
        else "le dio like a tu reseña."
    )
    db.add(
        Notification(
            recipient_user_id=review_owner_id,
            actor_user_id=actor_id,
            kind="like",
            target_review_id=review_id,
            text=text,
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
