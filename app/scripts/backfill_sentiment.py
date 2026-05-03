"""One-shot backfill: classify every review whose sentiment columns are
empty.

Run after migration 034 lands and ``GEMINI_API_KEY`` is set:

    python -m app.scripts.backfill_sentiment

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
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.dish import DishReview
from app.services.sentiment_service import analyze_review_text


BATCH_SIZE = 25  # Smaller than embeddings: each call is a separate Gemini request.
CONCURRENCY = 5  # Bound parallel Gemini calls so we don't hit RPM limits.

logger = logging.getLogger("backfill_sentiment")


async def _process_one(review: DishReview) -> bool:
    rating = float(review.rating) if review.rating is not None else None
    result = await analyze_review_text(review.note or "", rating=rating)
    if result is None:
        return False
    review.sentiment_label = result.label
    review.sentiment_score = Decimal(str(result.score))
    review.sentiment_analyzed_at = datetime.now(timezone.utc)
    return True


async def _backfill(reanalyze: bool) -> int:
    written = 0
    async with async_session() as db:
        stmt = select(DishReview).order_by(DishReview.created_at)
        if not reanalyze:
            stmt = stmt.where(DishReview.sentiment_analyzed_at.is_(None))
        reviews = list((await db.execute(stmt)).scalars().all())
        logger.info("Classifying %d reviews", len(reviews))

        sem = asyncio.Semaphore(CONCURRENCY)

        async def _bounded(rev: DishReview) -> bool:
            async with sem:
                return await _process_one(rev)

        for i in range(0, len(reviews), BATCH_SIZE):
            chunk = reviews[i : i + BATCH_SIZE]
            results = await asyncio.gather(
                *(_bounded(r) for r in chunk),
                return_exceptions=True,
            )
            for rev, ok in zip(chunk, results, strict=False):
                if isinstance(ok, BaseException):
                    logger.warning(
                        "Skipping review %s: %s", rev.id, ok
                    )
                    continue
                if ok:
                    written += 1
            await db.commit()
            logger.info(
                "  reviews %d/%d done (written=%d)",
                min(i + BATCH_SIZE, len(reviews)),
                len(reviews),
                written,
            )
    return written


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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if not settings.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set. Backfill cannot run.")
        sys.exit(2)

    written = await _backfill(reanalyze=args.reanalyze)
    logger.info("Done. reviews_written=%d", written)


if __name__ == "__main__":
    asyncio.run(main())
