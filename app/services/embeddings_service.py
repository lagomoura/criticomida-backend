"""Embeddings layer backed by Gemini ``gemini-embedding-2`` (768 dims).

Three surfaces:

- ``embed_query`` — single short text query, used live by the chatbot
  to rank ``search_dishes`` results when the user passes a free-form
  vibe.
- ``embed_documents`` — batched ingestion path used by the worker that
  fills ``dish_embeddings`` and ``dish_review_embeddings`` after a
  review is created/updated, plus the one-shot backfill script.
- ``embed_image`` — single image input (bytes + mime). Uses the same
  model: ``gemini-embedding-2`` is **natively multimodal**, so text
  documents and images map into the **same** 768-dim space. That's
  what lets the Sommelier compare a comensal-uploaded photo (image
  vector) against ``dish_embeddings`` (text vectors) by plain cosine
  distance — no separate "image embedding" table, no re-indexing.

The model is selected via ``settings.EMBEDDINGS_MODEL``. Output is
truncated to 768 dims with Matryoshka Representation Learning (MRL)
so the existing ``pgvector(768)`` schema works without migration even
though the model's native output is 3072.

Transport is the official ``google-genai`` SDK (no raw HTTP). When
``GEMINI_API_KEY`` is unset (typical in fresh dev), every call returns
``None``/empty so callers can degrade to structured-only search
without crashing.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
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


# Gemini caps a single ``embed_content`` call at ~100 items. Smaller
# keeps p99 latency reasonable on the live ``embed_query`` path even if
# it shares the helper.
_BATCH_LIMIT = 100


# Errors that mean "the provider call itself failed". google-genai
# wraps HTTP-level issues in ``APIError``; under the hood it uses
# ``httpx`` and can occasionally leak ``HTTPError`` (e.g. connection
# resets before the SDK can wrap them). We only catch this family so
# genuine programming errors (missing key after the guard, bad payload
# shape) still surface as 500s during development.
_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    genai_errors.APIError,
    httpx.HTTPError,
)


class EmbeddingsUnavailable(RuntimeError):
    """Raised when Gemini is not configured. Callers may swallow this and
    skip semantic features instead of erroring."""


def _api_key() -> str | None:
    return settings.GEMINI_API_KEY


_client: genai.Client | None = None


def _get_client() -> genai.Client | None:
    """Lazy singleton: don't construct a client when the API key is
    missing — fresh-dev environments must keep degrading gracefully."""
    global _client
    key = _api_key()
    if not key:
        return None
    if _client is None:
        _client = genai.Client(api_key=key)
    return _client


def _normalize_vector(vec: list[float]) -> list[float]:
    """Gemini already returns L2-normalized vectors for retrieval task
    types. Defensive guard: if a future version changes that, we still
    feed cosine-friendly vectors to pgvector.
    """
    norm = sum(x * x for x in vec) ** 0.5
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _coerce_embedding(
    embedding: genai_types.ContentEmbedding | None,
) -> list[float] | None:
    if embedding is None:
        return None
    values = embedding.values
    if not values or len(values) != EMBEDDING_DIMENSIONS:
        if values is not None:
            logger.warning(
                "Gemini embedding unexpected vector shape: %d dims",
                len(values),
            )
        return None
    return _normalize_vector(list(values))


async def embed_query(text: str) -> list[float] | None:
    """Embed a single query string. Returns ``None`` when Gemini is not
    configured so callers can fall back to structured ranking.
    """
    client = _get_client()
    if client is None or not text.strip():
        return None

    try:
        response = await client.aio.models.embed_content(
            model=settings.EMBEDDINGS_MODEL,
            contents=text,
            config=genai_types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=EMBEDDING_DIMENSIONS,
            ),
        )
    except _PROVIDER_ERRORS as exc:
        logger.warning("Gemini embed_query failed: %s", exc)
        return None

    embeddings = response.embeddings or []
    return _coerce_embedding(embeddings[0] if embeddings else None)


async def embed_documents(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch of document strings. Returns one vector per input
    in input order; entries the API rejected come back as ``None``.

    Sends one request per chunk of ``_BATCH_LIMIT`` to amortize HTTP
    overhead and stay under the per-call cap.
    """
    client = _get_client()
    if client is None:
        return [None] * len(texts)

    out: list[list[float] | None] = []
    for chunk_start in range(0, len(texts), _BATCH_LIMIT):
        chunk = texts[chunk_start : chunk_start + _BATCH_LIMIT]
        # Empty strings break batching on some embeddings models; keep
        # behaviour identical to the old REST path which substituted a
        # single-space placeholder for blank inputs.
        payload = [t if t else " " for t in chunk]
        try:
            response = await client.aio.models.embed_content(
                model=settings.EMBEDDINGS_MODEL,
                contents=payload,
                config=genai_types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",
                    output_dimensionality=EMBEDDING_DIMENSIONS,
                ),
            )
        except _PROVIDER_ERRORS as exc:
            logger.warning(
                "Gemini embed_documents batch failed at offset %d: %s",
                chunk_start,
                exc,
            )
            out.extend([None] * len(chunk))
            continue

        embeddings = response.embeddings or []
        for i in range(len(chunk)):
            entry = embeddings[i] if i < len(embeddings) else None
            out.append(_coerce_embedding(entry))

    return out


# ──────────────────────────────────────────────────────────────────────────
#   Multimodal — image embedding
# ──────────────────────────────────────────────────────────────────────────


async def embed_image(
    photo_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> list[float] | None:
    """Embed an image into the same 768-dim space as text documents.

    ``gemini-embedding-2`` is natively multimodal, so the resulting
    vector is directly comparable against ``dish_embeddings`` (which
    are text-derived) via cosine distance. No separate image-embedding
    table, no re-indexing of the catalog.

    Returns ``None`` when ``GEMINI_API_KEY`` is missing, the call
    fails, or the response shape is unexpected — callers should
    degrade gracefully (the photo tool falls back to text-embed of
    vision tags, and ultimately to a "vision_unavailable" message).

    ``task_type`` is intentionally NOT passed: the embeddings docs
    state ``gemini-embedding-2`` does not support that parameter for
    multimodal inputs (and ignores it for text). Including it here
    risked future breakage if Google starts validating the field.
    """
    client = _get_client()
    if client is None or not photo_bytes:
        return None

    part = genai_types.Part.from_bytes(
        data=photo_bytes, mime_type=mime_type or "image/jpeg"
    )
    try:
        response = await client.aio.models.embed_content(
            model=settings.EMBEDDINGS_MODEL,
            contents=[part],
            config=genai_types.EmbedContentConfig(
                output_dimensionality=EMBEDDING_DIMENSIONS,
            ),
        )
    except _PROVIDER_ERRORS as exc:
        logger.warning("Gemini embed_image failed: %s", exc)
        return None

    embeddings = response.embeddings or []
    return _coerce_embedding(embeddings[0] if embeddings else None)


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
    """Enqueue a re-embed job tied to ``review_id``.

    Persists into ``async_job`` inside the **caller's** transaction so
    the queued intent and the review row succeed or fail together. Was
    previously ``asyncio.create_task`` — that lost work on every Railway
    SIGTERM. The dedicated worker loop (``async_job_worker``) drains
    the queue with retry/backoff. Caller commits the surrounding
    transaction; we never commit here.
    """
    from app.models.async_job import AsyncJobKind
    from app.services.async_job_worker import enqueue

    await enqueue(db, kind=AsyncJobKind.embed_review, review_id=review_id)


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    bucket: list[Any] = []
    for it in items:
        bucket.append(it)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket
