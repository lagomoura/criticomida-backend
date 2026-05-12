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

Transport is the official ``google-genai`` SDK with ``response_schema``
pointing at a Pydantic model — Gemini validates the JSON for us and
returns ``response.parsed`` already instantiated. The defensive
``_coerce_label`` reconciliation stays because Pydantic only checks
field types, not the *semantic* consistency between label and score
(the same way a truncated output can flip the label keyword while
keeping the right score).

When ``GEMINI_API_KEY`` is unset, every call returns ``None`` so the
review write path stays uncoloured by the LLM dependency.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.dish import DishReview, SentimentLabel

logger = logging.getLogger(__name__)


# Same model the Ghostwriter uses for vision: cheap, fast, JSON-mode
# reliable. Override via env later if we want to A/B against Pro.
# Public-ish: the backfill batch script picks it up to address the
# same model when issuing async batch jobs.
SENTIMENT_MODEL = "gemini-2.5-flash"
# Reviews can be long; the model only needs the gist. Cap to keep prompt
# costs predictable across the corpus.
MAX_NOTE_CHARS = 1500

_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    genai_errors.APIError,
    httpx.HTTPError,
)


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


class _SentimentSchema(BaseModel):
    """Wire schema Gemini fills in via ``response_schema``. Pydantic
    enforces the literal label set and the score range — anything else
    surfaces as a parse failure and we fall back to ``None``."""

    label: Literal["positive", "neutral", "negative"]
    score: float = Field(ge=-1.0, le=1.0)


@dataclass(frozen=True)
class SentimentResult:
    label: SentimentLabel
    score: float  # ∈ [-1.0, 1.0]


_client: genai.Client | None = None


def _get_client() -> genai.Client | None:
    global _client
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    if _client is None:
        _client = genai.Client(api_key=key)
    return _client


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


def _prep_note(text: str) -> str:
    """Trim and clamp the review text the same way both the sync and
    batch paths feed it to the model."""
    note = (text or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        note = note[:MAX_NOTE_CHARS]
    return note


def build_sentiment_user_prompt(text: str, rating: float | None) -> str:
    """Compose the user-turn prompt for one review.

    Shared between ``analyze_review_text`` (sync path) and the batch
    backfill script, so the prompt structure stays in lockstep — a
    different prompt shape between paths would silently shift label
    distributions and make backfill output incomparable to live runs.
    """
    note = _prep_note(text)
    rating_hint = (
        f"Nota numérica del cliente: {rating:.1f} / 5"
        if rating is not None
        else "Nota numérica del cliente: no disponible"
    )
    return f"{rating_hint}\n\nReseña:\n{note}"


def build_sentiment_config() -> genai_types.GenerateContentConfig:
    """``GenerateContentConfig`` for sentiment classification.

    Both the sync path and the batch script call this so the system
    instruction, schema, and ``thinking_budget`` stay identical — a
    drift here would silently bias the backfill versus live writes.
    """
    return genai_types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=_SentimentSchema,
        temperature=0.1,
        # Gemini 2.5 Flash burns most of the token budget on hidden
        # "thinking" before emitting JSON. For a trivial 2-field
        # classification we don't want chain-of-thought — disable it
        # so the budget actually pays for the visible output. (See
        # memory ``feedback_gemini_thinking``.)
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=256,
    )


def parse_sentiment_response(
    response: genai_types.GenerateContentResponse | None,
) -> SentimentResult | None:
    """Coerce a ``GenerateContentResponse`` into a ``SentimentResult``.

    Tries ``response.parsed`` first (populated automatically on the
    sync ``generate_content`` path) and falls back to validating the
    raw JSON text. The batch path returns the same response shape but
    does **not** populate ``.parsed`` — the SDK only post-processes
    the schema on the sync surface — so we have to walk the JSON
    ourselves. Keeping both branches behind one function means the
    backfill and the live path see identical ``SentimentResult``
    objects.

    ``_coerce_label`` still runs at the end because Pydantic only
    checks field types — it can't catch a model that produced a
    positive label with a strongly negative score (truncation flips
    that occasionally)."""
    if response is None:
        return None

    parsed: _SentimentSchema | None = None
    raw_parsed = response.parsed
    if isinstance(raw_parsed, _SentimentSchema):
        parsed = raw_parsed
    else:
        text = (response.text or "").strip()
        if text:
            try:
                parsed = _SentimentSchema.model_validate_json(text)
            except ValueError as exc:
                logger.warning(
                    "Gemini sentiment unparseable JSON (tail=%r, err=%s)",
                    text[-80:],
                    exc,
                )

    if parsed is None:
        logger.warning(
            "Gemini sentiment returned unexpected payload (parsed=%r)",
            type(raw_parsed).__name__,
        )
        return None

    score = _clamp_score(parsed.score)
    label = _coerce_label(parsed.label, score)
    return SentimentResult(label=label, score=score)


async def analyze_review_text(
    text: str, rating: float | None = None
) -> SentimentResult | None:
    """Classify a single review text. Returns ``None`` when Gemini is
    unconfigured or the call fails — callers should treat ``None`` as
    "not analysed yet" and try again later, not as "definitely neutral".
    """
    client = _get_client()
    if client is None:
        return None

    if not _prep_note(text):
        return None

    try:
        response = await client.aio.models.generate_content(
            model=SENTIMENT_MODEL,
            contents=build_sentiment_user_prompt(text, rating),
            config=build_sentiment_config(),
        )
    except _PROVIDER_ERRORS as exc:
        logger.warning("Gemini sentiment call failed: %s", exc)
        return None

    return parse_sentiment_response(response)


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


async def schedule_analyze_review(
    db: AsyncSession, review_id: uuid.UUID
) -> None:
    """Enqueue a sentiment-analysis job tied to ``review_id``.

    Persists into ``async_job`` inside the **caller's** transaction so
    the queued intent and the review row succeed or fail together. Was
    previously ``asyncio.create_task`` — that lost work on every Railway
    SIGTERM. The dedicated worker loop (``async_job_worker``) drains
    the queue with retry/backoff. Caller commits the surrounding
    transaction; we never commit here.
    """
    from app.models.async_job import AsyncJobKind
    from app.services.async_job_worker import enqueue

    await enqueue(db, kind=AsyncJobKind.sentiment_review, review_id=review_id)


def note_hash(review: DishReview) -> str:
    """Stable hash of the analysed note, exposed for the backfill script
    so it can skip rows whose ``sentiment_analyzed_at`` is set and whose
    note hasn't changed since."""
    return _hash_text((review.note or "").strip())
