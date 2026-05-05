"""Insight tools for the Business agent (Phase 4).

Where ``list_reviews`` returns rows for the model to summarise,
``summarize_reviews_period`` returns the *summary itself* — averages,
distributions, deltas vs the prior period, response rate. The model
narrates; the SQL does the arithmetic. Two payoffs:

- **No more hallucinated percentages.** Counts and rates are computed
  by the database; the model only formats them.
- **Cheaper turns.** A 30-row review listing serialised as JSON
  consumes far more tokens than a single aggregate object — the
  model can cover wider time ranges without bloating the context.

Subsequent insight tools (``suggest_review_response``,
``compare_to_baseline``) will live here as F4.2 and F4.3.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import Dish, DishReview, SentimentLabel
from app.models.owner_content import DishReviewOwnerResponse
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._schemas import (
    ResponseTone,
    SuggestReviewResponseInput,
    SummarizeReviewsInput,
    SummaryDimension,
    pydantic_to_anthropic_schema,
)


# ──────────────────────────────────────────────────────────────────────────
#   summarize_reviews_period
# ──────────────────────────────────────────────────────────────────────────


def make_summarize_reviews_period_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        try:
            inputs = SummarizeReviewsInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for summarize_reviews_period.",
                "details": exc.errors(include_url=False),
            }

        if inputs.from_date > inputs.to_date:
            return {
                "error": (
                    "from_date must be on or before to_date "
                    f"(got {inputs.from_date} > {inputs.to_date})."
                )
            }

        period_days = (inputs.to_date - inputs.from_date).days + 1
        prior_to = inputs.from_date - timedelta(days=1)
        prior_from = prior_to - timedelta(days=period_days - 1)

        current = await _aggregate_period(
            db,
            restaurant_scope_id,
            inputs.from_date,
            inputs.to_date,
        )
        prior = await _aggregate_period(
            db,
            restaurant_scope_id,
            prior_from,
            prior_to,
        )

        result: dict[str, Any] = {
            "restaurant_id": restaurant_scope_id,
            "period": {
                "from": inputs.from_date.isoformat(),
                "to": inputs.to_date.isoformat(),
                "days": period_days,
            },
            "prior_period": {
                "from": prior_from.isoformat(),
                "to": prior_to.isoformat(),
            },
            "total_reviews": current["count"],
            "prior_total": prior["count"],
            "delta_count": current["count"] - prior["count"],
        }

        if SummaryDimension.rating in inputs.dimensions:
            result["rating"] = {
                "avg": current["rating_avg"],
                "prior_avg": prior["rating_avg"],
                "delta": _delta(current["rating_avg"], prior["rating_avg"]),
                "distribution": current["rating_distribution"],
            }

        if SummaryDimension.sentiment in inputs.dimensions:
            result["sentiment"] = {
                "by_label": current["sentiment_counts"],
                "avg_score": current["sentiment_avg"],
                "prior_avg_score": prior["sentiment_avg"],
                "delta_score": _delta(
                    current["sentiment_avg"], prior["sentiment_avg"]
                ),
            }

        if SummaryDimension.responded in inputs.dimensions:
            current_rate = (
                round(current["responded_count"] / current["count"], 3)
                if current["count"] > 0
                else None
            )
            prior_rate = (
                round(prior["responded_count"] / prior["count"], 3)
                if prior["count"] > 0
                else None
            )
            delta_pp: float | None = None
            if current_rate is not None and prior_rate is not None:
                delta_pp = round((current_rate - prior_rate) * 100, 1)
            result["responded"] = {
                "responded": current["responded_count"],
                "pending": current["count"] - current["responded_count"],
                "response_rate": current_rate,
                "prior_response_rate": prior_rate,
                "delta_pp": delta_pp,
            }

        return result

    return ToolSpec(
        name="summarize_reviews_period",
        description=(
            "Pre-calculated aggregates over the restaurant's reviews in a "
            "date period. Returns total count, rating average + "
            "distribution, sentiment breakdown + average score, and "
            "response rate, each with delta against the immediately "
            "prior period of the same length. **Use this instead of "
            "calling list_reviews and computing aggregates by hand** — "
            "the numbers here are authoritative and the LLM should never "
            "invent percentages. Compute the dates yourself in ISO "
            "format. Examples: 'cómo me fue en abril' → from='2026-04-01', "
            "to='2026-04-30'; 'última semana' → from=YYYY-MM-DD seven "
            "days back, to=today."
        ),
        input_schema=pydantic_to_anthropic_schema(SummarizeReviewsInput),
        handler=handler,
    )


# ──────────────────────────────────────────────────────────────────────────
#   Internal helpers
# ──────────────────────────────────────────────────────────────────────────


async def _aggregate_period(
    db: AsyncSession,
    restaurant_id: str,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """One pass over reviews in [from_date, to_date]. Aggregation is
    done in Python after the fetch — the volumes per restaurant per
    period are small (typically < 200 rows) so the simplicity wins
    over a multi-CTE SQL aggregate. If reviews scale 10x, swap this
    for a single GROUPING SET query."""
    stmt = (
        select(DishReview, DishReviewOwnerResponse.review_id)
        .join(Dish, DishReview.dish_id == Dish.id)
        .outerjoin(
            DishReviewOwnerResponse,
            DishReviewOwnerResponse.review_id == DishReview.id,
        )
        .where(
            Dish.restaurant_id == restaurant_id,
            func.date(DishReview.created_at) >= from_date,
            func.date(DishReview.created_at) <= to_date,
        )
    )
    rows = list((await db.execute(stmt)).all())

    count = len(rows)
    rating_values: list[float] = []
    sentiment_scores: list[float] = []
    sentiment_counts: dict[str, int] = {
        "positive": 0,
        "neutral": 0,
        "negative": 0,
        "unanalysed": 0,
    }
    rating_distribution: dict[str, int] = {}
    responded_count = 0

    for review, response_id in rows:
        if review.rating is not None:
            value = float(review.rating)
            rating_values.append(value)
            bucket = str(int(round(value)))
            rating_distribution[bucket] = rating_distribution.get(bucket, 0) + 1
        if review.sentiment_score is not None:
            sentiment_scores.append(float(review.sentiment_score))
        label = review.sentiment_label
        if label is None:
            sentiment_counts["unanalysed"] += 1
        else:
            sentiment_counts[label.value] += 1
        if response_id is not None:
            responded_count += 1

    return {
        "count": count,
        "rating_avg": round(sum(rating_values) / len(rating_values), 2)
        if rating_values
        else None,
        "rating_distribution": rating_distribution,
        "sentiment_avg": round(sum(sentiment_scores) / len(sentiment_scores), 2)
        if sentiment_scores
        else None,
        "sentiment_counts": sentiment_counts,
        "responded_count": responded_count,
    }


def _delta(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    return round(current - prior, 2)


# ──────────────────────────────────────────────────────────────────────────
#   suggest_review_response
# ──────────────────────────────────────────────────────────────────────────


# Tone-by-tone guidance the agent applies when drafting. We keep this in
# Spanish neutral so the prompt template doesn't have to translate
# anything — the LLM reads the guidance, the LLM writes the draft in
# the language the review was originally in. ``match_brand`` is a stub
# until F5 (owner preferences) lands; for now it falls back to
# ``professional`` semantics with a note explaining why.
_TONE_GUIDANCE: dict[ResponseTone, str] = {
    ResponseTone.warm: (
        "Tono cálido y cercano. Empezá agradeciendo con palabras "
        "específicas a lo que el cliente menciona. Mostrá que leíste su "
        "comentario en concreto, no genérico. Cerrá con una invitación "
        "abierta a volver."
    ),
    ResponseTone.professional: (
        "Tono profesional y cortés. Frases cortas, sin diminutivos ni "
        "informalismos. Reconocé el feedback puntual. No hagas promesas "
        "que requieran aprobación del owner."
    ),
    ResponseTone.apologetic: (
        "Tono empático que reconoce el problema. Empezá pidiendo "
        "disculpas con una sola frase, sin sobreactuar. Validá lo "
        "específico que el cliente mencionó. Cerrá ofreciendo seguimiento "
        "(no compensación material — eso lo decide el owner)."
    ),
    ResponseTone.match_brand: (
        "Sin perfil de marca todavía registrado para este restaurante "
        "(roadmap F5). Caigo a tono profesional cortés. Mencioná esto al "
        "owner como nota cuando le presentes el draft."
    ),
}


def _infer_tone(sentiment: SentimentLabel | None) -> ResponseTone:
    """Pick a sensible tone from the review sentiment when the LLM
    didn't pass one explicitly. Negative sentiment → apologise;
    positive → warm; neutral or unanalysed → professional default."""
    if sentiment is SentimentLabel.negative:
        return ResponseTone.apologetic
    if sentiment is SentimentLabel.positive:
        return ResponseTone.warm
    return ResponseTone.professional


_REPLY_HARD_RULES = [
    "Nunca prometas un cambio concreto que requiera aprobación del owner "
    "(devolver dinero, cambiar la receta, etc.).",
    "No menciones que sos un agente de IA ni que esto es un draft "
    "automático — escribilo como si fuera el owner respondiendo.",
    "Mantené la respuesta entre 2 y 5 frases. El cliente lee desde el "
    "móvil; los párrafos largos se ignoran.",
    "Respondé en el MISMO idioma del texto original de la reseña, no en "
    "el idioma del owner. Si la reseña está en portugués, la respuesta "
    "va en portugués.",
]


def make_suggest_review_response_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        try:
            inputs = SuggestReviewResponseInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for suggest_review_response.",
                "details": exc.errors(include_url=False),
            }

        try:
            review_uuid = uuid.UUID(inputs.review_id)
        except (TypeError, ValueError):
            return {
                "error": (
                    f"review_id={inputs.review_id!r} is not a valid UUID. "
                    "Use a review_id from a previous list_reviews call."
                )
            }

        stmt = (
            select(DishReview, Dish, DishReviewOwnerResponse.review_id)
            .join(Dish, DishReview.dish_id == Dish.id)
            .outerjoin(
                DishReviewOwnerResponse,
                DishReviewOwnerResponse.review_id == DishReview.id,
            )
            .where(
                DishReview.id == review_uuid,
                Dish.restaurant_id == restaurant_scope_id,
            )
            .options(selectinload(DishReview.dish))
        )
        result = (await db.execute(stmt)).first()
        if result is None:
            return {
                "error": (
                    f"Review {inputs.review_id} not found within this "
                    "restaurant's scope. Re-check with list_reviews — "
                    "the review may belong to a different restaurant or "
                    "may have been deleted."
                )
            }

        review, dish, existing_response_id = result

        if existing_response_id is not None:
            # Already responded to — flag clearly so the agent can
            # surface that to the owner instead of silently overwriting.
            return {
                "review_id": str(review.id),
                "already_responded": True,
                "note": (
                    "Esta reseña ya tiene respuesta del owner registrada. "
                    "Confirmá con el owner si quiere reemplazarla antes "
                    "de redactar un draft nuevo."
                ),
            }

        # Resolve tone: if the LLM passed one, use it; otherwise infer
        # from the review's sentiment so the tool stays useful even when
        # the model omits the optional parameter (Gemini Flash Lite
        # tends to skip optionals — pragmatic recovery beats forcing it
        # to retry).
        effective_tone = inputs.tone or _infer_tone(review.sentiment_label)
        tone_was_inferred = inputs.tone is None

        return {
            "review_id": str(review.id),
            "already_responded": False,
            "review": {
                "note": review.note,
                "rating": float(review.rating)
                if review.rating is not None
                else None,
                "created_at": review.created_at.isoformat(),
                "sentiment_label": (
                    review.sentiment_label.value
                    if review.sentiment_label is not None
                    else None
                ),
            },
            "dish": {
                "id": str(dish.id),
                "name": dish.name,
            },
            "tone": effective_tone.value,
            "tone_inferred_from_sentiment": tone_was_inferred,
            "tone_guidance": _TONE_GUIDANCE[effective_tone],
            "language_hint": (
                "Detectá el idioma del campo review.note y respondé en "
                "ese idioma exactamente. No traduzcas el draft al idioma "
                "del owner."
            ),
            "must_not": _REPLY_HARD_RULES,
            "format": (
                "Después de leer este payload, redactá el draft directo "
                "en tu próximo mensaje al owner. Empezá con una línea "
                "corta tipo 'Te propongo este draft:' y luego el draft "
                "entre comillas o con sangría markdown. NO llames otros "
                "tools antes de presentarlo."
            ),
        }

    return ToolSpec(
        name="suggest_review_response",
        description=(
            "Prepares structured context for drafting a reply to a "
            "specific review. Returns the review text + dish info + tone "
            "guidance + hard rules. **You** (the agent) write the actual "
            "draft in the next assistant turn using this payload — there "
            "is no separate LLM call inside the tool. The ``review_id`` "
            "argument MUST come from a prior ``list_reviews`` result; "
            "never ask the owner for it. If the review already has an "
            "owner response, the tool returns ``already_responded: true`` "
            "so you can confirm with the owner before writing a new one."
        ),
        input_schema=pydantic_to_anthropic_schema(SuggestReviewResponseInput),
        handler=handler,
    )
