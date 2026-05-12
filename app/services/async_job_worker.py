"""Worker loop that drains the ``async_job`` table.

Why a queue instead of ``asyncio.create_task``: Railway sends SIGTERM
on every redeploy and uvicorn does not wait for in-flight coroutines
spawned via ``asyncio.create_task``. Reviews created seconds before a
deploy used to lose their embedding/sentiment write silently. The
queue persists the intent inside the same DB transaction as the
review, so a restart resumes work instead of dropping it.

Operational notes:

- The worker is a single asyncio task launched in
  ``app.main.production_lifespan``. Multiple uvicorn workers each
  start their own loop; the ``UPDATE ... RETURNING`` claim with
  ``FOR UPDATE SKIP LOCKED`` guarantees at-most-one process picks
  any given row.
- Failures bump ``attempts`` and re-schedule with linear backoff up
  to ``MAX_ATTEMPTS``. After that, the row is parked as ``failed``
  and ignored — operator can re-queue manually if needed.
- The loop is opt-in via ``settings.ASYNC_JOB_WORKER_ENABLED`` so
  tests and the alembic-only entrypoints don't spin it up.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.async_job import AsyncJob, AsyncJobKind, AsyncJobStatus

logger = logging.getLogger(__name__)


# How long we wait after an empty poll before checking again. Short
# enough that a freshly-enqueued job feels instantaneous to the user
# (the embedding lands within a second or two of the review write),
# long enough that an idle DB doesn't get hammered.
_IDLE_POLL_SECONDS = 1.5

# Linear backoff baseline for retries.
_RETRY_BACKOFF_SECONDS = 30
_MAX_ATTEMPTS = 5


async def _claim_one(db: AsyncSession) -> AsyncJob | None:
    """Atomically claim the next pending job.

    Uses ``UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED)
    RETURNING *`` so two workers (multi-worker uvicorn) never grab
    the same row. Without ``SKIP LOCKED`` they'd block on each other
    and serialize.
    """
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(
            text(
                """
                UPDATE async_job
                   SET status = 'running',
                       started_at = :now,
                       attempts = attempts + 1
                 WHERE id = (
                       SELECT id FROM async_job
                        WHERE status = 'pending'
                          AND scheduled_at <= :now
                        ORDER BY scheduled_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                       )
             RETURNING id, kind, payload_review_id, attempts;
                """
            ),
            {"now": now},
        )
    ).first()
    if row is None:
        await db.commit()
        return None
    await db.commit()

    # Re-fetch as ORM so the handler can use SQLAlchemy semantics if
    # it wants (we still pass the raw fields below).
    job = await db.get(AsyncJob, row.id)
    return job


async def _mark_done(db: AsyncSession, job: AsyncJob) -> None:
    job.status = AsyncJobStatus.done
    job.completed_at = datetime.now(timezone.utc)
    job.last_error = None
    await db.commit()


async def _mark_retry_or_fail(
    db: AsyncSession, job: AsyncJob, error: BaseException
) -> None:
    if job.attempts >= _MAX_ATTEMPTS:
        job.status = AsyncJobStatus.failed
        job.completed_at = datetime.now(timezone.utc)
    else:
        # Re-queue with linear backoff. Status goes back to pending so
        # the partial index keeps the row indexed for pickup.
        job.status = AsyncJobStatus.pending
        job.scheduled_at = datetime.now(timezone.utc) + timedelta(
            seconds=_RETRY_BACKOFF_SECONDS * job.attempts
        )
    job.last_error = f"{type(error).__name__}: {error}"[:2000]
    await db.commit()


async def _run_job(db: AsyncSession, job: AsyncJob) -> None:
    """Dispatch by ``kind``. Imported lazily to avoid a circular import
    via ``embeddings_service`` → ``async_job_worker``.

    Each branch knows which payload columns to read — the
    ``ck_async_job_payload_shape`` CHECK in migration 063 guarantees
    the right ones are populated for each kind.
    """
    if job.kind is AsyncJobKind.embed_review:
        from app.services.embeddings_service import reembed_review

        await reembed_review(db, job.payload_review_id)
    elif job.kind is AsyncJobKind.sentiment_review:
        from app.services.sentiment_service import analyze_and_persist_review

        await analyze_and_persist_review(db, job.payload_review_id)
    elif job.kind is AsyncJobKind.sommelier_review_recall:
        from app.services.sommelier_recall_service import (
            process_sommelier_review_recall,
        )

        await process_sommelier_review_recall(
            db,
            user_id=job.payload_user_id,
            dish_id=job.payload_dish_id,
        )
    else:  # pragma: no cover — enum exhaustive
        raise RuntimeError(f"Unknown async_job kind: {job.kind!r}")


async def _drain_one() -> bool:
    """Process at most one job. Returns True if something was claimed."""
    async with async_session() as db:
        try:
            job = await _claim_one(db)
        except Exception:
            logger.exception("async_job claim failed; backing off")
            return False
        if job is None:
            return False
        try:
            await _run_job(db, job)
        except Exception as exc:
            await db.rollback()
            try:
                # Refetch after rollback so the SQLAlchemy state is clean.
                fresh = await db.get(AsyncJob, job.id)
                if fresh is not None:
                    await _mark_retry_or_fail(db, fresh, exc)
            except Exception:
                logger.exception("async_job retry-bookkeeping failed")
            else:
                logger.warning(
                    "async_job %s (%s) failed attempt %d: %s",
                    job.id,
                    job.kind.value,
                    job.attempts,
                    exc,
                )
            return True
        await _mark_done(db, job)
        return True


async def run_worker_loop(stop_event: asyncio.Event) -> None:
    """Long-running loop. Exits when ``stop_event`` is set."""
    logger.info("async_job worker started")
    while not stop_event.is_set():
        try:
            did_work = await _drain_one()
        except Exception:
            logger.exception("async_job worker loop iteration crashed")
            did_work = False
        if not did_work:
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=_IDLE_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                pass
    logger.info("async_job worker stopping")


# ──────────────────────────────────────────────────────────────────────────
#   Enqueue helpers
# ──────────────────────────────────────────────────────────────────────────


async def enqueue(
    db: AsyncSession,
    *,
    kind: AsyncJobKind,
    review_id,
) -> None:
    """Insert a pending job, deduplicating against an already-pending row.

    Caller is responsible for committing — the enqueue lives inside the
    same transaction that wrote the review, so the queue and the data
    succeed or fail together.
    """
    # Partial unique index on (kind, payload_review_id) WHERE status =
    # 'pending' guarantees at most one queued job per (kind, review).
    # The ``ON CONFLICT (cols) WHERE ...`` form must match the index's
    # predicate exactly for Postgres to use the index as the arbiter.
    await db.execute(
        text(
            """
            INSERT INTO async_job (id, kind, payload_review_id, status, scheduled_at, created_at)
            VALUES (gen_random_uuid(), :kind, :review_id, 'pending', now(), now())
            ON CONFLICT (kind, payload_review_id) WHERE status = 'pending'
            DO NOTHING;
            """
        ),
        {"kind": kind.value, "review_id": review_id},
    )
