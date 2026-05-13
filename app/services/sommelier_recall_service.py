"""Sommelier review-recall: enqueue and processing.

End-to-end flow:

1. The Sommelier calls ``recommend_dishes(dish_ids=[...])`` for an
   authenticated diner.
2. The tool handler invokes :func:`enqueue_sommelier_review_recalls`,
   which writes one row per dish into ``async_job`` with a
   ``scheduled_at`` 24h in the future (configurable via
   ``SOMMELIER_RECALL_DELAY_HOURS``). A partial UNIQUE index on
   ``(kind, payload_user_id, payload_dish_id) WHERE status='pending'
   AND kind='sommelier_review_recall'`` makes the insert idempotent
   if the agent re-recommends the same dish before the first recall
   fires — second insert collapses into the first pending row.
3. When ``scheduled_at`` arrives, the worker
   (:mod:`app.services.async_job_worker`) claims the row and calls
   :func:`process_sommelier_review_recall`.
4. The handler defends idempotency in two more layers:
   - If the diner already reviewed the dish, mark done without
     inserting a notification.
   - If a notification with the same ``(recipient, target_dish_id)``
     already exists (because the user re-triggered the recall later
     in another conversation), skip the insert.
   Then it goes through the standard ``should_deliver_notification``
   guard (block/mute) and finally writes the row.

The actor for the notification is a deterministic system user
(``SOMMELIER_BOT_USER_ID``, seeded by migration 063). The bot row
has an unusable password hash so no login path can mint a session
for it; it exists purely to satisfy ``notifications.actor_user_id``
NOT NULL without us having to schema-change a NOT NULL FK that
already has thousands of rows.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.dish import Dish, DishReview
from app.models.restaurant import Restaurant
from app.models.social import Notification
from app.services.safety_service import should_deliver_notification


logger = logging.getLogger(__name__)


# Deterministic UUID for the Sommelier bot user (seeded in migration
# 063). Last 12 hex chars = ASCII "Palato".
SOMMELIER_BOT_USER_ID: uuid.UUID = uuid.UUID(
    "00000000-0000-4000-8000-50616c61746f"
)

_RECALL_KIND: str = "sommelier_review_recall"
_JOB_KIND: str = "sommelier_review_recall"

# Empty-state preview tuning. Lookback caps at 14 days because past
# that window the diner's memory of the meal decays enough that a
# "did you try it?" prompt feels like noise. Limit caps at 3 cards to
# avoid wrapping the section on a 360px-wide phone.
_PREVIEW_LOOKBACK_DAYS: int = 14
_PREVIEW_LIMIT: int = 3


@dataclass(frozen=True)
class PendingRecallItem:
    """One pending review-recall surfaced in the Sommelier empty state.

    Hydrated from a JOIN over ``async_job``, ``dishes`` and
    ``restaurants`` so the FE can paint the card in a single fetch.
    """

    dish_id: uuid.UUID
    dish_name: str
    cover_image_url: str | None
    restaurant_name: str
    restaurant_slug: str | None
    recommended_at: datetime


async def get_pending_recalls(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    limit: int = _PREVIEW_LIMIT,
    lookback_days: int = _PREVIEW_LOOKBACK_DAYS,
) -> list[PendingRecallItem]:
    """Return the diner's most recent pending recalls.

    Source of truth is ``async_job``: every Sommelier recommendation
    enqueues a row with ``kind='sommelier_review_recall'`` and
    ``payload_user_id``/``payload_dish_id``. The Post-visit Bridge (B)
    surfaces those that are still pending — i.e. the diner didn't
    write the review yet — so the empty state can prompt them to
    close the loop on their next chat open.

    Ordering: most recent first (memory of the meal is freshest).
    DISTINCT ON collapses the case where the Sommelier recommended the
    same dish in two different conversations — one card per dish, not
    one per recommendation event.
    """
    if limit <= 0:
        return []

    # DISTINCT ON requires ORDER BY to start with the distinct column,
    # so we keep the dedup inside a subquery and sort the outer
    # SELECT by recency. ``make_interval`` keeps the int parameter
    # type-safe under asyncpg (the same coercion bug that bit the
    # enqueue path; see migration 063 / commit history).
    # Two NOT EXISTS guards: (1) already reviewed, (2) explicitly
    # dismissed via the "X" on the empty-state card. Both must miss
    # for the dish to surface — either signal means "diner already
    # closed the loop on this", just for different reasons.
    stmt = text(
        """
        SELECT
            sub.dish_id,
            sub.dish_name,
            sub.cover_image_url,
            sub.restaurant_name,
            sub.restaurant_slug,
            sub.recommended_at
        FROM (
            SELECT DISTINCT ON (j.payload_dish_id)
                j.payload_dish_id AS dish_id,
                d.name AS dish_name,
                d.cover_image_url AS cover_image_url,
                r.name AS restaurant_name,
                r.slug AS restaurant_slug,
                j.created_at AS recommended_at
            FROM async_job j
            JOIN dishes d ON d.id = j.payload_dish_id
            JOIN restaurants r ON r.id = d.restaurant_id
            WHERE j.kind = 'sommelier_review_recall'
              AND j.payload_user_id = :user_id
              AND j.created_at > now() - make_interval(days => :lookback_days)
              AND NOT EXISTS (
                  SELECT 1 FROM dish_reviews dr
                  WHERE dr.user_id = j.payload_user_id
                    AND dr.dish_id = j.payload_dish_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM sommelier_recall_dismissals dm
                  WHERE dm.user_id = j.payload_user_id
                    AND dm.dish_id = j.payload_dish_id
              )
            ORDER BY j.payload_dish_id, j.created_at DESC
        ) sub
        ORDER BY sub.recommended_at DESC
        LIMIT :limit;
        """
    )
    rows = (
        await db.execute(
            stmt,
            {
                "user_id": str(user_id),
                "lookback_days": lookback_days,
                "limit": limit,
            },
        )
    ).all()
    return [
        PendingRecallItem(
            dish_id=row.dish_id,
            dish_name=row.dish_name,
            cover_image_url=row.cover_image_url,
            restaurant_name=row.restaurant_name,
            restaurant_slug=row.restaurant_slug,
            recommended_at=row.recommended_at,
        )
        for row in rows
    ]


async def dismiss_pending_recall(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dish_id: uuid.UUID,
) -> None:
    """Mark a (user, dish) pair as dismissed from the pending-recalls
    surface. Idempotent: ON CONFLICT DO NOTHING means a repeated
    dismiss from a flaky network is a silent no-op.

    Caller controls the commit so the dismiss rides the same tx as
    any audit logging that wraps the endpoint.
    """
    await db.execute(
        text(
            """
            INSERT INTO sommelier_recall_dismissals (
                user_id, dish_id, dismissed_at
            ) VALUES (:user_id, :dish_id, now())
            ON CONFLICT (user_id, dish_id) DO NOTHING;
            """
        ),
        {"user_id": str(user_id), "dish_id": str(dish_id)},
    )


async def _filter_dishes_already_reviewed(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dish_ids: Iterable[uuid.UUID],
) -> list[uuid.UUID]:
    """Return the subset of ``dish_ids`` the user has NOT reviewed yet.

    Saves us a queue row for every dish the diner already wrote up.
    Cheaper to filter here than to enqueue a job that's destined to
    be a no-op when it fires 24h later.
    """
    ids = list(dish_ids)
    if not ids:
        return []
    rows = await db.execute(
        select(DishReview.dish_id)
        .where(DishReview.user_id == user_id)
        .where(DishReview.dish_id.in_(ids))
    )
    reviewed = {r[0] for r in rows.all()}
    return [d for d in ids if d not in reviewed]


async def enqueue_sommelier_review_recalls(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dish_ids: Iterable[uuid.UUID],
    delay_hours: int | None = None,
) -> int:
    """Enqueue one delayed recall job per dish, returning the count
    actually inserted (after pre-filtering already-reviewed dishes
    and after the partial UNIQUE index collapses duplicates).

    Caller controls the commit — the enqueue rides the same
    transaction as the chat message persistence so we don't end up
    with phantom jobs for conversations that ultimately rolled back.
    """
    requested = list(dish_ids)
    candidate_dish_ids = await _filter_dishes_already_reviewed(
        db, user_id=user_id, dish_ids=requested
    )
    if not candidate_dish_ids:
        logger.info(
            "sommelier_recall enqueue skip: user %s — all %d dishes "
            "already reviewed",
            user_id,
            len(requested),
        )
        return 0

    hours = (
        delay_hours
        if delay_hours is not None
        else settings.SOMMELIER_RECALL_DELAY_HOURS
    )
    scheduled_offset = timedelta(hours=hours)

    inserted = 0
    for dish_id in candidate_dish_ids:
        # ON CONFLICT DO NOTHING leans on
        # ``ix_async_job_pending_recall_dedup``: at most one pending
        # recall per (user, dish). If a previous conversation already
        # left one in the queue, the second insert is a no-op.
        # ``make_interval(hours => :hours)`` takes a typed int directly
        # — avoids the string-concat coercion that asyncpg rejects when
        # we pass an int parameter into ``(:hours || ' hours')::interval``.
        result = await db.execute(
            text(
                """
                INSERT INTO async_job (
                    id, kind, payload_user_id, payload_dish_id,
                    status, scheduled_at, created_at
                ) VALUES (
                    gen_random_uuid(),
                    'sommelier_review_recall',
                    :user_id,
                    :dish_id,
                    'pending',
                    now() + make_interval(hours => :hours),
                    now()
                )
                ON CONFLICT (kind, payload_user_id, payload_dish_id)
                    WHERE status = 'pending'
                            AND kind = 'sommelier_review_recall'
                DO NOTHING;
                """
            ),
            {
                "user_id": str(user_id),
                "dish_id": str(dish_id),
                "hours": hours,
            },
        )
        # ``rowcount`` is 1 on insert, 0 on conflict.
        if result.rowcount:
            inserted += 1
    logger.info(
        "sommelier_recall enqueue: user=%s requested=%d candidates=%d "
        "inserted=%d delay_h=%d",
        user_id,
        len(requested),
        len(candidate_dish_ids),
        inserted,
        hours,
    )
    return inserted


async def _build_recall_text(
    db: AsyncSession, *, dish_id: uuid.UUID
) -> str | None:
    """Build the denormalized ``notifications.text`` for one recall.

    Shape: ``"<dish_name> · <restaurant_name>"``. Returns ``None`` if
    the dish disappeared between the enqueue and the run (handler
    should treat that as "nothing to recall, mark done").
    """
    row = (
        await db.execute(
            select(Dish)
            .where(Dish.id == dish_id)
            .options(selectinload(Dish.restaurant))
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    restaurant_name = row.restaurant.name if row.restaurant else None
    if restaurant_name:
        return f"{row.name} · {restaurant_name}"
    return row.name


async def _recall_notification_already_exists(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dish_id: uuid.UUID,
) -> bool:
    """Defensive idempotency: don't notify twice for the same (user, dish).

    The partial UNIQUE index on async_job prevents duplicate *pending*
    rows, but not duplicate notifications across the lifetime of the
    feature (a job can complete, get cleaned up, and a fresh recall
    can be enqueued months later). The handler checks the
    notifications table directly via
    ``ix_notifications_recall_dedup``.
    """
    result = await db.execute(
        select(Notification.id)
        .where(Notification.recipient_user_id == user_id)
        .where(Notification.kind == _RECALL_KIND)
        .where(Notification.target_dish_id == dish_id)
        .limit(1)
    )
    return result.first() is not None


async def process_sommelier_review_recall(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dish_id: uuid.UUID,
) -> None:
    """Worker entrypoint. Idempotent — safe to retry on transient errors.

    Caller (the async_job worker) wraps the call in its own commit
    semantics; we just stage the insert.
    """
    # 1. Already reviewed? Loop closed elsewhere — drop the recall.
    reviewed = (
        await db.execute(
            select(DishReview.id)
            .where(DishReview.user_id == user_id)
            .where(DishReview.dish_id == dish_id)
            .limit(1)
        )
    ).first()
    if reviewed is not None:
        logger.debug(
            "sommelier_recall skip: user %s already reviewed dish %s",
            user_id,
            dish_id,
        )
        return

    # 2. Already notified for this (user, dish)? Defensive — partial
    #    unique index on pending jobs doesn't cover this.
    if await _recall_notification_already_exists(
        db, user_id=user_id, dish_id=dish_id
    ):
        logger.debug(
            "sommelier_recall skip: notification already exists "
            "for user %s dish %s",
            user_id,
            dish_id,
        )
        return

    # 3. Standard safety guard. The bot user never blocks or mutes
    #    anyone in practice, but routing through the same guard keeps
    #    the contract uniform with the rest of notification_service.
    if not await should_deliver_notification(
        db,
        recipient_id=user_id,
        actor_id=SOMMELIER_BOT_USER_ID,
    ):
        logger.debug(
            "sommelier_recall skip: should_deliver=False for user %s",
            user_id,
        )
        return

    # 4. Resolve the denormalized text. ``None`` means the dish was
    #    deleted between enqueue and run — drop the recall silently.
    body = await _build_recall_text(db, dish_id=dish_id)
    if body is None:
        logger.info(
            "sommelier_recall skip: dish %s no longer exists",
            dish_id,
        )
        return

    db.add(
        Notification(
            recipient_user_id=user_id,
            actor_user_id=SOMMELIER_BOT_USER_ID,
            kind=_RECALL_KIND,
            target_dish_id=dish_id,
            text=body,
        )
    )
