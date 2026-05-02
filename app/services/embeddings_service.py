"""Embeddings layer backed by Gemini ``text-embedding-004`` (768 dims).

Two surfaces:

- ``embed_query`` — single short string, used live by the chatbot to
  rank ``search_dishes`` results when the user passes a free-form vibe.
- ``embed_documents`` — batched ingestion path used by the worker that
  fills ``dish_embeddings`` and ``dish_review_embeddings`` after a
  review is created/updated, plus the one-shot backfill script.

When ``GEMINI_API_KEY`` is unset (typical in fresh dev), every call
returns ``None``/empty so callers can degrade to structured-only search
without crashing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Iterable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.chat import (
    EMBEDDING_DIMENSIONS,
    DishEmbedding,
    DishReviewEmbedding,
)
from app.models.dish import Dish, DishReview

logger = logging.getLogger(__name__)


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Gemini caps single batch at ~100 items. Smaller keeps p99 latency
# reasonable on the live `embed_query` path even if it shares the helper.
_BATCH_LIMIT = 100
_REQUEST_TIMEOUT = 20.0


class EmbeddingsUnavailable(RuntimeError):
    """Raised when Gemini is not configured. Callers may swallow this and
    skip semantic features instead of erroring."""


def _api_key() -> str | None:
    return settings.GEMINI_API_KEY


def _normalize_vector(vec: list[float]) -> list[float]:
    """Gemini already returns L2-normalized vectors for retrieval task
    types. Defensive guard: if a future version changes that, we still
    feed cosine-friendly vectors to pgvector.
    """
    norm = sum(x * x for x in vec) ** 0.5
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


async def embed_query(text: str) -> list[float] | None:
    """Embed a single query string. Returns ``None`` when Gemini is not
    configured so callers can fall back to structured ranking.
    """
    key = _api_key()
    if not key or not text.strip():
        return None

    # gemini-embedding-001 is Matryoshka-aware: requesting 768 dims keeps
    # our pgvector schema compatible without a migration.
    url = f"{_GEMINI_BASE}/models/{settings.EMBEDDINGS_MODEL}:embedContent"
    payload: dict[str, Any] = {
        "model": f"models/{settings.EMBEDDINGS_MODEL}",
        "content": {"parts": [{"text": text}]},
        "taskType": "RETRIEVAL_QUERY",
        "outputDimensionality": EMBEDDING_DIMENSIONS,
    }
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(
                url,
                params={"key": key},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            vec = data.get("embedding", {}).get("values")
            if not vec or len(vec) != EMBEDDING_DIMENSIONS:
                logger.warning(
                    "embed_query unexpected vector shape: %d dims",
                    len(vec) if vec else 0,
                )
                return None
            return _normalize_vector(vec)
    except httpx.HTTPError as exc:
        logger.warning("Gemini embed_query failed: %s", exc)
        return None


async def embed_documents(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch of document strings. Returns one vector per input
    in input order; entries the API rejected come back as ``None``.

    Uses the ``batchEmbedContents`` endpoint to amortize HTTP overhead.
    Splits into chunks of ``_BATCH_LIMIT`` automatically.
    """
    key = _api_key()
    if not key:
        return [None] * len(texts)

    url = f"{_GEMINI_BASE}/models/{settings.EMBEDDINGS_MODEL}:batchEmbedContents"
    out: list[list[float] | None] = []

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for chunk_start in range(0, len(texts), _BATCH_LIMIT):
            chunk = texts[chunk_start : chunk_start + _BATCH_LIMIT]
            payload = {
                "requests": [
                    {
                        "model": f"models/{settings.EMBEDDINGS_MODEL}",
                        "content": {"parts": [{"text": t or " "}]},
                        "taskType": "RETRIEVAL_DOCUMENT",
                        "outputDimensionality": EMBEDDING_DIMENSIONS,
                    }
                    for t in chunk
                ]
            }
            try:
                r = await client.post(url, params={"key": key}, json=payload)
                r.raise_for_status()
                data = r.json()
                for entry in data.get("embeddings", []):
                    vec = entry.get("values")
                    if vec and len(vec) == EMBEDDING_DIMENSIONS:
                        out.append(_normalize_vector(vec))
                    else:
                        out.append(None)
                # Pad in case the API skipped trailing items.
                while len(out) < chunk_start + len(chunk):
                    out.append(None)
            except httpx.HTTPError as exc:
                logger.warning(
                    "Gemini embed_documents batch failed at offset %d: %s",
                    chunk_start,
                    exc,
                )
                out.extend([None] * len(chunk))

    return out


# ──────────────────────────────────────────────────────────────────────────
#   DB persistence helpers
# ──────────────────────────────────────────────────────────────────────────


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review_text(review: DishReview) -> str:
    pros = " | ".join(
        pc.text for pc in (review.pros_cons or []) if pc.type.value == "pro"
    )
    cons = " | ".join(
        pc.text for pc in (review.pros_cons or []) if pc.type.value == "con"
    )
    tags = " ".join(t.tag for t in (review.tags or []))
    parts = [review.note or "", pros, cons, tags]
    return " ".join(p for p in parts if p).strip()


def _dish_aggregate_text(dish: Dish) -> str:
    parts: list[str] = [dish.name]
    if dish.description:
        parts.append(dish.description)
    if dish.editorial_blurb:
        parts.append(dish.editorial_blurb)
    # Include the top-3 reviews by rating so the dish vector reflects the
    # actual eating experience, not just marketing copy.
    sorted_reviews = sorted(
        dish.reviews or [],
        key=lambda r: float(r.rating or 0),
        reverse=True,
    )[:3]
    for r in sorted_reviews:
        if r.note:
            parts.append(r.note)
    return " ".join(parts).strip()


async def reembed_review(db: AsyncSession, review_id: uuid.UUID) -> None:
    """Refresh the embedding for a single review and its parent dish.

    Called from the dish-review router after a write commits. Failures
    are logged and swallowed — embedding misses degrade search but must
    never block a user-facing review write.
    """
    stmt = (
        select(DishReview)
        .where(DishReview.id == review_id)
        .options(
            selectinload(DishReview.pros_cons),
            selectinload(DishReview.tags),
        )
    )
    review = (await db.execute(stmt)).scalars().first()
    if review is None:
        return

    text = _review_text(review)
    if not text:
        return

    vectors = await embed_documents([text])
    vec = vectors[0] if vectors else None
    if vec is None:
        return

    upsert = (
        insert(DishReviewEmbedding)
        .values(dish_review_id=review.id, embedding=vec)
        .on_conflict_do_update(
            index_elements=["dish_review_id"],
            set_={"embedding": vec},
        )
    )
    await db.execute(upsert)

    await reembed_dish(db, review.dish_id)


async def reembed_dish(db: AsyncSession, dish_id: uuid.UUID) -> None:
    stmt = (
        select(Dish)
        .where(Dish.id == dish_id)
        .options(selectinload(Dish.reviews))
    )
    dish = (await db.execute(stmt)).scalars().first()
    if dish is None:
        return

    text = _dish_aggregate_text(dish)
    if not text:
        return

    text_hash = _hash_text(text)

    existing = (
        await db.execute(
            select(DishEmbedding).where(DishEmbedding.dish_id == dish_id)
        )
    ).scalars().first()
    if existing and existing.source_text_hash == text_hash:
        return  # Nothing changed — skip the API call.

    vectors = await embed_documents([text])
    vec = vectors[0] if vectors else None
    if vec is None:
        return

    upsert = (
        insert(DishEmbedding)
        .values(dish_id=dish_id, embedding=vec, source_text_hash=text_hash)
        .on_conflict_do_update(
            index_elements=["dish_id"],
            set_={"embedding": vec, "source_text_hash": text_hash},
        )
    )
    await db.execute(upsert)


async def schedule_reembed_review(
    db: AsyncSession, review_id: uuid.UUID
) -> None:
    """Fire-and-forget wrapper safe to call from request handlers.

    The work is real (not just queued) but the request returns regardless
    of outcome. Until we add a proper background queue, we run it inline
    on a detached task tied to the event loop.
    """

    async def _run() -> None:
        try:
            await reembed_review(db, review_id)
            await db.commit()
        except Exception:
            logger.exception("schedule_reembed_review failed")

    asyncio.create_task(_run())


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    bucket: list[Any] = []
    for it in items:
        bucket.append(it)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket
