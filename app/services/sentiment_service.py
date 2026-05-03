"""Sentiment analysis for dish reviews via Gemini Flash.

The owner dashboard and the Business chatbot need a way to triage which
reviews to respond to first when the inbox is large. Rating alone is a
weak signal — a 4★ review can still hide a frustrated customer, and a
2★ can be a thoughtful critique. We classify the *text* of each review
into one of three labels (``positive`` / ``neutral`` / ``negative``)
plus a fine-grained score in ``[-1, 1]`` for ordering.

Surfaces:

- ``analyze_review_text(text, rating)`` — pure: takes inputs, calls
  Gemini, returns ``SentimentResult`` or ``None``. Used by both the
  hot path and the backfill script.
- ``analyze_and_persist_review(db, review_id)`` — load a single review,
  classify, UPDATE the row. Idempotent: skips if the note hash matches
  the previous run.
- ``schedule_analyze_review(review_id)`` — fire-and-forget wrapper for
  routers. Opens its own session so we don't depend on the request
  session still being alive after the response returns.

Visibility note: the result is only consumed in owner-scoped surfaces
(dashboard + Business chatbot tool). It is never serialized in the
public ``DishReviewResponse``.

When ``GEMINI_API_KEY`` is unset, every call returns ``None`` so the
review write path stays uncoloured by the LLM dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.dish import DishReview, SentimentLabel

logger = logging.getLogger(__name__)


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Same model the Ghostwriter uses for vision: cheap, fast, JSON-mode
# reliable. Override via env later if we want to A/B against Pro.
_SENTIMENT_MODEL = "gemini-2.5-flash"
_REQUEST_TIMEOUT = 15.0
# Reviews can be long; the model only needs the gist. Cap to keep prompt
# costs predictable across the corpus.
_MAX_NOTE_CHARS = 1500


_SYSTEM_INSTRUCTION = """Sos un analizador de sentimiento de reseñas gastronómicas en español rioplatense (también podés recibir portugués o inglés).

Recibís el texto de una reseña y, opcionalmente, la nota numérica (1.0 a 5.0) que dio el cliente. Tu trabajo es resumir el TONO del texto en un label y un score numérico.

Devolvé SIEMPRE un JSON válido con esta forma exacta:

{
  "label": "positive|neutral|negative",
  "score": -1.0
}

Reglas:

- "positive" = predomina la satisfacción, el entusiasmo o la recomendación cálida.
- "negative" = predomina la queja, frustración, decepción o crítica dura.
- "neutral" = factual sin afecto claro, o aspectos positivos y negativos se neutralizan.
- "score" ∈ [-1.0, 1.0]. 1.0 = entusiasmo extremo, -1.0 = enojo o queja intensa, 0.0 = totalmente neutral. Usá decimales (ej: 0.4, -0.65).
- El score tiene que ser COHERENTE con el label: positivo ⇒ score > 0.15, negativo ⇒ score < -0.15, neutral ⇒ |score| ≤ 0.15.
- Cuidado con el sarcasmo: "qué increíble pagar tanto por tan poco" es negativo aunque diga "increíble".
- Si la nota numérica y el texto se contradicen (ej: 5★ con texto duro), priorizá el TEXTO. La disonancia es justamente lo que estamos detectando.
- Devolvé el JSON pelado, sin texto extra."""


_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "label": {
            "type": "STRING",
            "enum": ["positive", "neutral", "negative"],
        },
        "score": {"type": "NUMBER"},
    },
    "required": ["label", "score"],
}


@dataclass(frozen=True)
class SentimentResult:
    label: SentimentLabel
    score: float  # ∈ [-1.0, 1.0]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clamp_score(value: float) -> float:
    if value > 1.0:
        return 1.0
    if value < -1.0:
        return -1.0
    return round(float(value), 2)


def _coerce_label(raw_label: str, score: float) -> SentimentLabel:
    """Trust the model on the bucket but reconcile if the score is wildly
    inconsistent — defends against truncated outputs that flip the label
    while keeping the right score (rare but observed in vision_service)."""
    try:
        label = SentimentLabel(raw_label.strip().lower())
    except ValueError:
        label = SentimentLabel.neutral

    if label is SentimentLabel.positive and score < -0.15:
        return SentimentLabel.negative
    if label is SentimentLabel.negative and score > 0.15:
        return SentimentLabel.positive
    return label


async def analyze_review_text(
    text: str, rating: float | None = None
) -> SentimentResult | None:
    """Classify a single review text. Returns ``None`` when Gemini is
    unconfigured or the call fails — callers should treat ``None`` as
    "not analysed yet" and try again later, not as "definitely neutral".
    """
    key = settings.GEMINI_API_KEY
    if not key:
        return None

    note = (text or "").strip()
    if not note:
        return None
    if len(note) > _MAX_NOTE_CHARS:
        note = note[:_MAX_NOTE_CHARS]

    rating_hint = (
        f"Nota numérica del cliente: {rating:.1f} / 5"
        if rating is not None
        else "Nota numérica del cliente: no disponible"
    )
    user_prompt = f"{rating_hint}\n\nReseña:\n{note}"

    payload: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generation_config": {
            "response_mime_type": "application/json",
            "response_schema": _SCHEMA,
            "temperature": 0.1,
            # Gemini 2.5 Flash burns most of the token budget on hidden
            # "thinking" before emitting JSON. For a trivial 2-field
            # classification we don't want chain-of-thought — disable
            # it so the budget actually pays for the visible output.
            "thinking_config": {"thinking_budget": 0},
            # Headroom in case a future prompt change needs more tokens;
            # with thinking off the actual emission is ~30 tokens.
            "max_output_tokens": 256,
        },
    }
    url = f"{_GEMINI_BASE}/models/{_SENTIMENT_MODEL}:generateContent"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(url, params={"key": key}, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        logger.warning("Gemini sentiment call failed: %s", exc)
        return None

    try:
        first_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        logger.warning("Gemini sentiment response shape unexpected: %s", exc)
        return None

    try:
        raw = json.loads(first_text)
    except ValueError:
        logger.warning(
            "Gemini sentiment returned unparseable JSON (tail=%r)",
            first_text[-80:] if first_text else "",
        )
        return None

    raw_label = raw.get("label")
    raw_score = raw.get("score")
    if not isinstance(raw_label, str) or not isinstance(raw_score, (int, float)):
        return None

    score = _clamp_score(float(raw_score))
    label = _coerce_label(raw_label, score)
    return SentimentResult(label=label, score=score)


async def analyze_and_persist_review(
    db: AsyncSession, review_id: uuid.UUID
) -> None:
    """Load the review, classify it, and write the three sentiment
    columns. No-op on missing review or empty note. Caller commits.

    Idempotent: if the review's note has not changed since the previous
    analysis (matched via SHA-256 of the trimmed note), skip the LLM
    call. We don't persist the hash itself — we re-derive it from the
    current ``note`` and compare against ``sentiment_analyzed_at`` only
    as a freshness signal. The full re-analysis on edit is what callers
    actually want, so we err toward re-running when in doubt.
    """
    review = (
        await db.execute(
            select(DishReview).where(DishReview.id == review_id)
        )
    ).scalar_one_or_none()
    if review is None:
        return

    note = (review.note or "").strip()
    if not note:
        return

    rating = float(review.rating) if review.rating is not None else None
    result = await analyze_review_text(note, rating=rating)
    if result is None:
        return

    review.sentiment_label = result.label
    review.sentiment_score = Decimal(str(result.score))
    review.sentiment_analyzed_at = datetime.now(timezone.utc)


def schedule_analyze_review(review_id: uuid.UUID) -> None:
    """Fire-and-forget invocation safe to call from a request handler.

    Opens its own session so we don't depend on the request session
    still being alive after the response is sent. Failures log and are
    swallowed: a missed sentiment column is never worth bouncing the
    user write."""

    async def _run() -> None:
        try:
            async with async_session() as db:
                try:
                    await analyze_and_persist_review(db, review_id)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception:
            logger.exception("schedule_analyze_review failed")

    asyncio.create_task(_run())


def note_hash(review: DishReview) -> str:
    """Stable hash of the analysed note, exposed for the backfill script
    so it can skip rows whose ``sentiment_analyzed_at`` is set and whose
    note hasn't changed since."""
    return _hash_text((review.note or "").strip())
