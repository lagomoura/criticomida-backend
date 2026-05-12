"""One-shot backfill: classify every review whose sentiment columns are
empty.

Default path is the **Gemini Batch API** — same model, same prompt,
same schema as the live path, but ~50% cheaper at the cost of a 24 h
SLA. The script blocks until the batch completes (or the operator
kills it); the batch's ``name`` is logged at the start, so a killed
run can be resumed manually by passing ``--resume <batch_name>``.

    python -m app.scripts.backfill_sentiment             # batch path
    python -m app.scripts.backfill_sentiment --sync      # per-request
    python -m app.scripts.backfill_sentiment --limit 50  # smoke / dry-run

Idempotent — re-runs are safe. Skips reviews where
``sentiment_analyzed_at`` is already populated unless ``--reanalyze``
is passed (then every row is re-classified, useful when the prompt
changes materially).

Fails loudly when Gemini is not configured: a successful exit with no
rows written would be misleading.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.dish import DishReview
from app.services.sentiment_service import (
    SENTIMENT_MODEL,
    SentimentResult,
    analyze_review_text,
    build_sentiment_config,
    build_sentiment_user_prompt,
    parse_sentiment_response,
)


# --- Sync path tuning (when --sync is passed) -----------------------------
SYNC_BATCH_SIZE = 25
SYNC_CONCURRENCY = 5

# --- Batch path tuning ----------------------------------------------------
# Inlined batch jobs have a per-job cap (~100 MB request body in the
# current docs). Reviews are short — 1500 chars trimmed — so 1000
# requests per job is well under any cap and gives operators
# meaningful progress checkpoints when the corpus is large.
BATCH_CHUNK_SIZE = 1000
# Polling cadence: a sentiment batch usually completes in well under
# the 24h SLA, but the worst case is the SLA. Poll every 60s so the
# script doesn't flood the API; print progress every poll.
POLL_INTERVAL_SECONDS = 60
# Hard ceiling. Past this, we log the batch name and bail — the
# operator can resume with ``--resume <name>``.
POLL_MAX_SECONDS = 25 * 3600  # 25h, slightly past the documented SLA

_TERMINAL_OK = {
    genai_types.JobState.JOB_STATE_SUCCEEDED,
    genai_types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED,
}
_TERMINAL_FAIL = {
    genai_types.JobState.JOB_STATE_FAILED,
    genai_types.JobState.JOB_STATE_CANCELLED,
    genai_types.JobState.JOB_STATE_EXPIRED,
}


logger = logging.getLogger("backfill_sentiment")


# --------------------------------------------------------------------------
#   Loading + persistence (shared by both paths)
# --------------------------------------------------------------------------


@dataclass
class _ReviewSnapshot:
    """Captured before the batch is built. We persist into DB by id at
    the end, so the ORM objects don't need to stay attached to a
    session for the (possibly hours-long) batch wait."""

    id: uuid.UUID
    note: str
    rating: float | None


async def _load_targets(reanalyze: bool, limit: int | None) -> list[_ReviewSnapshot]:
    async with async_session() as db:
        stmt = select(DishReview).order_by(DishReview.created_at)
        if not reanalyze:
            stmt = stmt.where(DishReview.sentiment_analyzed_at.is_(None))
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
    snapshots: list[_ReviewSnapshot] = []
    for r in rows:
        note = (r.note or "").strip()
        if not note:
            # Empty notes never produced a result on the sync path
            # either — skip them so we don't waste batch slots.
            continue
        snapshots.append(
            _ReviewSnapshot(
                id=r.id,
                note=note,
                rating=float(r.rating) if r.rating is not None else None,
            )
        )
    return snapshots


async def _persist_result(
    review_id: uuid.UUID, result: SentimentResult
) -> bool:
    """UPDATE one review with the sentiment columns. Commits per row so
    a long batch can be interrupted without losing earlier work."""
    async with async_session() as db:
        review = (
            await db.execute(
                select(DishReview).where(DishReview.id == review_id)
            )
        ).scalar_one_or_none()
        if review is None:
            return False
        review.sentiment_label = result.label
        review.sentiment_score = Decimal(str(result.score))
        review.sentiment_analyzed_at = datetime.now(timezone.utc)
        await db.commit()
        return True


# --------------------------------------------------------------------------
#   Sync path (kept as escape hatch when batch is unavailable)
# --------------------------------------------------------------------------


async def _process_one_sync(snap: _ReviewSnapshot) -> bool:
    result = await analyze_review_text(snap.note, rating=snap.rating)
    if result is None:
        return False
    return await _persist_result(snap.id, result)


async def _run_sync(snapshots: list[_ReviewSnapshot]) -> int:
    written = 0
    sem = asyncio.Semaphore(SYNC_CONCURRENCY)

    async def _bounded(snap: _ReviewSnapshot) -> bool:
        async with sem:
            return await _process_one_sync(snap)

    for i in range(0, len(snapshots), SYNC_BATCH_SIZE):
        chunk = snapshots[i : i + SYNC_BATCH_SIZE]
        results = await asyncio.gather(
            *(_bounded(s) for s in chunk),
            return_exceptions=True,
        )
        for snap, ok in zip(chunk, results, strict=False):
            if isinstance(ok, BaseException):
                logger.warning("Skipping review %s: %s", snap.id, ok)
                continue
            if ok:
                written += 1
        logger.info(
            "  reviews %d/%d done (written=%d)",
            min(i + SYNC_BATCH_SIZE, len(snapshots)),
            len(snapshots),
            written,
        )
    return written


# --------------------------------------------------------------------------
#   Batch path
# --------------------------------------------------------------------------


def _build_inlined_requests(
    snapshots: list[_ReviewSnapshot],
) -> list[genai_types.InlinedRequest]:
    config = build_sentiment_config()
    return [
        genai_types.InlinedRequest(
            model=SENTIMENT_MODEL,
            contents=build_sentiment_user_prompt(snap.note, snap.rating),
            config=config,
        )
        for snap in snapshots
    ]


async def _wait_for_batch(
    client: genai.Client, batch_name: str
) -> genai_types.BatchJob:
    """Poll the batch until it reaches a terminal state. Logs progress
    every ``POLL_INTERVAL_SECONDS``."""
    started = time.monotonic()
    while True:
        batch = await client.aio.batches.get(name=batch_name)
        state = batch.state
        elapsed = int(time.monotonic() - started)
        logger.info(
            "  batch %s state=%s elapsed=%ds",
            batch_name,
            state.name if state is not None else "?",
            elapsed,
        )
        if state in _TERMINAL_OK:
            return batch
        if state in _TERMINAL_FAIL:
            err_msg = getattr(batch.error, "message", None) if batch.error else None
            raise RuntimeError(
                f"batch {batch_name} ended in {state.name}: {err_msg or 'no error message'}"
            )
        if elapsed > POLL_MAX_SECONDS:
            raise RuntimeError(
                f"batch {batch_name} exceeded max wait ({POLL_MAX_SECONDS}s). "
                f"Resume later with --resume {batch_name}"
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _consume_batch(
    batch: genai_types.BatchJob, snapshots: list[_ReviewSnapshot]
) -> int:
    """Walk ``batch.dest.inlined_responses`` in lockstep with the input
    snapshots. The SDK preserves input order, so position i in the
    output maps to ``snapshots[i]``."""
    if batch.dest is None or batch.dest.inlined_responses is None:
        logger.error(
            "batch %s succeeded but has no inlined_responses",
            batch.name,
        )
        return 0
    responses = batch.dest.inlined_responses
    if len(responses) != len(snapshots):
        # PARTIALLY_SUCCEEDED can return fewer responses; lockstep would
        # mis-align labels with reviews. Bail loudly so an operator can
        # rerun rather than poisoning the corpus.
        logger.error(
            "batch %s returned %d responses for %d requests — refusing to persist",
            batch.name,
            len(responses),
            len(snapshots),
        )
        return 0

    written = 0
    for snap, inlined in zip(snapshots, responses, strict=True):
        if inlined.error is not None:
            logger.warning(
                "review %s: batch entry errored — %s",
                snap.id,
                getattr(inlined.error, "message", inlined.error),
            )
            continue
        result = parse_sentiment_response(inlined.response)
        if result is None:
            continue
        try:
            if await _persist_result(snap.id, result):
                written += 1
        except Exception:
            logger.exception("Persist failed for review %s", snap.id)
    return written


async def _run_batch_chunk(
    client: genai.Client,
    chunk: list[_ReviewSnapshot],
    chunk_index: int,
    resume_name: str | None,
) -> int:
    """Submit one batch job for a chunk, wait for terminal state, persist
    the responses, return how many rows were written."""
    if resume_name is not None:
        logger.info("Resuming batch %s for chunk %d", resume_name, chunk_index)
        batch = await client.aio.batches.get(name=resume_name)
    else:
        display_name = f"sentiment-backfill-{int(time.time())}-{chunk_index:04d}"
        logger.info(
            "Submitting batch %s (%d requests)",
            display_name,
            len(chunk),
        )
        try:
            batch = await client.aio.batches.create(
                model=SENTIMENT_MODEL,
                src=_build_inlined_requests(chunk),
                config=genai_types.CreateBatchJobConfig(
                    display_name=display_name,
                ),
            )
        except genai_errors.APIError as exc:
            logger.error(
                "Batch creation failed for chunk %d: %s. "
                "Fall back with --sync or fix the underlying issue.",
                chunk_index,
                exc,
            )
            return 0
        logger.info("  → batch name: %s", batch.name)

    try:
        batch = await _wait_for_batch(client, batch.name)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 0

    return await _consume_batch(batch, chunk)


async def _run_batch(
    snapshots: list[_ReviewSnapshot], resume_name: str | None
) -> int:
    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    if resume_name is not None:
        # Resume mode targets exactly the snapshots we just loaded. The
        # operator is responsible for passing the same ``--reanalyze``
        # flag so the snapshot list matches the original batch.
        return await _run_batch_chunk(client, snapshots, 0, resume_name)

    total_written = 0
    for i in range(0, len(snapshots), BATCH_CHUNK_SIZE):
        chunk = snapshots[i : i + BATCH_CHUNK_SIZE]
        chunk_index = i // BATCH_CHUNK_SIZE
        written = await _run_batch_chunk(client, chunk, chunk_index, None)
        total_written += written
        logger.info(
            "  chunk %d done (written=%d, cumulative=%d)",
            chunk_index,
            written,
            total_written,
        )
    return total_written


# --------------------------------------------------------------------------
#   CLI
# --------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help=(
            "Re-classify every review even if it already has a sentiment "
            "set. Use after a material prompt change."
        ),
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Use the legacy per-request path (Semaphore-bounded) instead "
            "of the async Batch API. Slower and ~2x more expensive, but "
            "needed if the Batch API is unavailable for this model."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N matching reviews (smoke / dry-run).",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help=(
            "Resume waiting on an existing batch job by its full name "
            "(e.g. 'batches/abc123'). Requires --reanalyze / --limit to "
            "match the original invocation so the snapshot list aligns."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if not settings.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set. Backfill cannot run.")
        sys.exit(2)

    snapshots = await _load_targets(args.reanalyze, args.limit)
    logger.info("Classifying %d reviews", len(snapshots))
    if not snapshots:
        return

    if args.sync:
        if args.resume:
            logger.error("--resume is only meaningful in batch mode (drop --sync)")
            sys.exit(2)
        written = await _run_sync(snapshots)
    else:
        written = await _run_batch(snapshots, args.resume)

    logger.info("Done. reviews_written=%d", written)


if __name__ == "__main__":
    asyncio.run(main())
