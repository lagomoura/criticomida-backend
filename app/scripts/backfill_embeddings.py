"""One-shot backfill: embed every dish and every review in the catalog.

Run after the Phase 0 migration lands and ``GEMINI_API_KEY`` is set:

    python -m app.scripts.backfill_embeddings

Idempotent — re-runs are safe. ``dish_embeddings`` skip when the source
text hash matches the previous run, and ``dish_review_embeddings`` upsert
on the primary key.

Fails loudly if Gemini is not configured: backfill without a working API
key produces no rows but a successful exit, which is misleading.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session
from app.models.chat import (
    EMBEDDING_DIMENSIONS,
    DishEmbedding,
    DishReviewEmbedding,
)
from app.models.dish import Dish, DishReview
from app.services.embeddings_service import (
    _dish_aggregate_text,
    _hash_text,
    _review_text,
    embed_documents,
)


BATCH_SIZE = 50
logger = logging.getLogger("backfill_embeddings")


async def _backfill_reviews() -> int:
    written = 0
    async with async_session() as db:
        stmt = (
            select(DishReview)
            .options(
                selectinload(DishReview.pros_cons),
                selectinload(DishReview.tags),
            )
            .order_by(DishReview.created_at)
        )
        reviews = list((await db.execute(stmt)).scalars().unique().all())
        logger.info("Embedding %d reviews", len(reviews))

        for i in range(0, len(reviews), BATCH_SIZE):
            chunk = reviews[i : i + BATCH_SIZE]
            texts = [_review_text(r) for r in chunk]
            vectors = await embed_documents(texts)
            for review, vec in zip(chunk, vectors):
                if vec is None:
                    continue
                if len(vec) != EMBEDDING_DIMENSIONS:
                    logger.warning(
                        "Skipping review %s: bad vector size", review.id
                    )
                    continue
                upsert = (
                    insert(DishReviewEmbedding)
                    .values(dish_review_id=review.id, embedding=vec)
                    .on_conflict_do_update(
                        index_elements=["dish_review_id"],
                        set_={"embedding": vec},
                    )
                )
                await db.execute(upsert)
                written += 1
            await db.commit()
            logger.info("  reviews %d/%d done", min(i + BATCH_SIZE, len(reviews)), len(reviews))
    return written


async def _backfill_dishes() -> int:
    written = 0
    async with async_session() as db:
        stmt = (
            select(Dish)
            .options(selectinload(Dish.reviews))
            .order_by(Dish.created_at)
        )
        dishes = list((await db.execute(stmt)).scalars().unique().all())
        logger.info("Embedding %d dishes", len(dishes))

        # Drop dishes whose hash hasn't changed.
        existing = {
            row.dish_id: row.source_text_hash
            for row in (
                await db.execute(select(DishEmbedding))
            ).scalars().all()
        }
        pending: list[tuple[Dish, str, str]] = []  # dish, text, hash
        for dish in dishes:
            text = _dish_aggregate_text(dish)
            if not text:
                continue
            h = _hash_text(text)
            if existing.get(dish.id) == h:
                continue
            pending.append((dish, text, h))

        logger.info("  %d dishes need re-embedding", len(pending))

        for i in range(0, len(pending), BATCH_SIZE):
            chunk = pending[i : i + BATCH_SIZE]
            vectors = await embed_documents([t for _, t, _ in chunk])
            for (dish, _, h), vec in zip(chunk, vectors):
                if vec is None or len(vec) != EMBEDDING_DIMENSIONS:
                    continue
                upsert = (
                    insert(DishEmbedding)
                    .values(
                        dish_id=dish.id,
                        embedding=vec,
                        source_text_hash=h,
                    )
                    .on_conflict_do_update(
                        index_elements=["dish_id"],
                        set_={"embedding": vec, "source_text_hash": h},
                    )
                )
                await db.execute(upsert)
                written += 1
            await db.commit()
            logger.info("  dishes %d/%d done", min(i + BATCH_SIZE, len(pending)), len(pending))
    return written


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    if not settings.GEMINI_API_KEY:
        logger.error(
            "GEMINI_API_KEY is not set. Backfill cannot run."
        )
        sys.exit(2)

    reviews_written = await _backfill_reviews()
    dishes_written = await _backfill_dishes()
    logger.info(
        "Done. reviews=%d dishes=%d", reviews_written, dishes_written
    )


if __name__ == "__main__":
    asyncio.run(main())
