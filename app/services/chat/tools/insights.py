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
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._schemas import (
    BaselineKind,
    BaselineMetric,
    CompareToBaselineInput,
    ResponseTone,
    SuggestReviewResponseInput,
    SummarizeReviewsInput,
    SummaryDimension,
    UpdateOwnerPreferencesInput,
    pydantic_to_anthropic_schema,
)
from app.services.owner_chat_preferences_service import (
    get_chat_preferences,
    upsert_chat_preference,
)
# Reuse the geometry + percentile helpers already battle-tested in
# benchmark_dish — same restaurant-discovery logic, same fairness
# semantics for ranking.
from app.services.chat.tools.business import _haversine_km, _percentile


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
    from_date: date | None,
    to_date: date | None,
) -> dict[str, Any]:
    """One pass over reviews in [from_date, to_date]. Aggregation is
    done in Python after the fetch — the volumes per restaurant per
    period are small (typically < 200 rows) so the simplicity wins
    over a multi-CTE SQL aggregate. If reviews scale 10x, swap this
    for a single GROUPING SET query.

    Passing ``None`` for both dates aggregates over the whole history
    of the restaurant — used by ``compare_to_baseline`` with
    ``vs='all_time'``.
    """
    stmt = (
        select(DishReview, DishReviewOwnerResponse.review_id)
        .join(Dish, DishReview.dish_id == Dish.id)
        .outerjoin(
            DishReviewOwnerResponse,
            DishReviewOwnerResponse.review_id == DishReview.id,
        )
        .where(Dish.restaurant_id == restaurant_id)
    )
    if from_date is not None:
        stmt = stmt.where(func.date(DishReview.created_at) >= from_date)
    if to_date is not None:
        stmt = stmt.where(func.date(DishReview.created_at) <= to_date)
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
}


_MATCH_BRAND_FALLBACK_GUIDANCE = (
    "Sin perfil de tono persistente registrado para este owner. "
    "Cayendo a tono profesional cortés. El owner puede fijar uno con "
    "el tool update_owner_preferences si quiere que futuras respuestas "
    "lo respeten."
)


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
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    restaurant_scope_id: str | None,
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
        # to retry). ``match_brand`` resolves to the owner's persisted
        # preference when one exists.
        effective_tone = inputs.tone or _infer_tone(review.sentiment_label)
        tone_was_inferred = inputs.tone is None
        guidance_override: str | None = None

        if effective_tone is ResponseTone.match_brand:
            persisted = None
            if user_id is not None:
                try:
                    restaurant_uuid = uuid.UUID(restaurant_scope_id)
                    prefs = await get_chat_preferences(
                        db,
                        user_id=user_id,
                        restaurant_id=restaurant_uuid,
                    )
                    if prefs and prefs.tone_preference:
                        try:
                            persisted = ResponseTone(prefs.tone_preference)
                        except ValueError:
                            # Preference stores a tone value that isn't
                            # in our reply enum (e.g. ``concise``);
                            # treat as "no usable preference".
                            persisted = None
                except (TypeError, ValueError):
                    persisted = None
            if persisted is not None and persisted is not ResponseTone.match_brand:
                effective_tone = persisted
            else:
                effective_tone = ResponseTone.professional
                guidance_override = _MATCH_BRAND_FALLBACK_GUIDANCE

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
            "tone_guidance": guidance_override or _TONE_GUIDANCE[effective_tone],
            "language_hint": (
                "Detectá el idioma del campo review.note y respondé en "
                "ese idioma exactamente. No traduzcas el draft al idioma "
                "del owner."
            ),
            "must_not": _REPLY_HARD_RULES,
            "format": (
                "Después de leer este payload, en tu próximo mensaje al "
                "owner: (1) Citá la reseña original primero en texto "
                "plano markdown (NO blockquote): "
                "'**Reseña:** {dish.name} · {review.rating}★ · "
                "{review.created_at fecha corta}' en una línea y abajo "
                "_\"{review.note recortado a ~280 chars}\"_ en italics. "
                "Si review.note es null, escribí _\"Sin comentario "
                "escrito\"_. (2) UNA línea corta de intro tipo 'Te "
                "propongo este draft:'. (3) El draft mismo EN UN "
                "MARKDOWN BLOCKQUOTE: cada línea del draft empieza con "
                "'> '. **El blockquote es exclusivo del draft** — la FE "
                "extrae el primer blockquote del mensaje para "
                "pre-cargarlo en el modal de respuesta. Si citás la "
                "reseña con '>' o usás triple-fence/HTML, el botón "
                "'Responder esta reseña' termina con texto que no es "
                "el draft. NO llames otros tools antes de presentarlo."
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


# ──────────────────────────────────────────────────────────────────────────
#   compare_to_baseline
# ──────────────────────────────────────────────────────────────────────────


_METRIC_LABELS_ES = {
    BaselineMetric.rating: "rating promedio",
    BaselineMetric.review_count: "cantidad de reseñas",
    BaselineMetric.sentiment_score: "score de sentimiento",
    BaselineMetric.response_rate: "tasa de respuesta",
}

_BASELINE_LABELS_ES = {
    BaselineKind.prior_period: "período anterior de igual duración",
    BaselineKind.all_time: "promedio histórico del restaurante",
    BaselineKind.competition: "promedio del entorno geográfico",
}


def make_compare_to_baseline_tool(
    db: AsyncSession, *, restaurant_scope_id: str | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if restaurant_scope_id is None:
            return {"error": "Business scope is required."}

        try:
            inputs = CompareToBaselineInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for compare_to_baseline.",
                "details": exc.errors(include_url=False),
            }

        # Defaults when the LLM omits the period — last 30 days ending
        # today. Saves the model from guessing dates and racing through
        # iterations correcting itself.
        today = date.today()
        eff_to = inputs.to_date or today
        eff_from = inputs.from_date or (eff_to - timedelta(days=29))

        if eff_from > eff_to:
            return {
                "error": (
                    "from_date must be on or before to_date "
                    f"(got {eff_from} > {eff_to})."
                )
            }

        if (
            inputs.vs is BaselineKind.competition
            and inputs.metric is BaselineMetric.sentiment_score
        ):
            return {
                "error": (
                    "competition mode does not support sentiment_score "
                    "(most peers don't have analysed sentiment, the "
                    "comparison would be misleading). Use rating, "
                    "review_count, or response_rate instead."
                )
            }

        # Current period aggregate
        current = await _aggregate_period(
            db,
            restaurant_scope_id,
            eff_from,
            eff_to,
        )
        current_value = _extract_metric(current, inputs.metric)
        if current_value is None:
            return {
                "metric": inputs.metric.value,
                "comparison_kind": inputs.vs.value,
                "period": {
                    "from": eff_from.isoformat(),
                    "to": eff_to.isoformat(),
                },
                "current": {"value": None, "sample_size": current["count"]},
                "note": (
                    "El restaurante no tiene reseñas en el período pedido, "
                    "no hay valor actual para comparar."
                ),
            }

        # Baseline branch
        if inputs.vs is BaselineKind.prior_period:
            period_days = (eff_to - eff_from).days + 1
            prior_to = eff_from - timedelta(days=1)
            prior_from = prior_to - timedelta(days=period_days - 1)
            prior = await _aggregate_period(
                db, restaurant_scope_id, prior_from, prior_to
            )
            baseline_value = _extract_metric(prior, inputs.metric)
            baseline_payload: dict[str, Any] = {
                "kind": "prior_period",
                "label": _BASELINE_LABELS_ES[BaselineKind.prior_period],
                "value": baseline_value,
                "sample_size": prior["count"],
                "from": prior_from.isoformat(),
                "to": prior_to.isoformat(),
            }
            extras: dict[str, Any] = {}
        elif inputs.vs is BaselineKind.all_time:
            all_time = await _aggregate_period(
                db, restaurant_scope_id, None, None
            )
            baseline_value = _extract_metric(all_time, inputs.metric)
            baseline_payload = {
                "kind": "all_time",
                "label": _BASELINE_LABELS_ES[BaselineKind.all_time],
                "value": baseline_value,
                "sample_size": all_time["count"],
            }
            extras = {}
        else:  # BaselineKind.competition
            cohort = await _competition_metric(
                db,
                restaurant_scope_id,
                inputs.metric,
                eff_from,
                eff_to,
                inputs.radius_km,
            )
            if cohort["cohort_size"] < 3:
                return {
                    "metric": inputs.metric.value,
                    "comparison_kind": inputs.vs.value,
                    "period": {
                        "from": eff_from.isoformat(),
                        "to": eff_to.isoformat(),
                    },
                    "current": {
                        "value": current_value,
                        "sample_size": current["count"],
                    },
                    "note": (
                        f"Sólo {cohort['cohort_size']} competidores en el "
                        f"radio de {inputs.radius_km} km tienen datos en "
                        "este período — la cohort es muy chica para hablar "
                        "de percentil. Sugerí ampliar el radio."
                    ),
                    "cohort_size": cohort["cohort_size"],
                    "radius_km": inputs.radius_km,
                }
            baseline_value = cohort["cohort_avg"]
            baseline_payload = {
                "kind": "competition",
                "label": _BASELINE_LABELS_ES[BaselineKind.competition],
                "value": baseline_value,
                "sample_size": cohort["cohort_size"],
            }
            extras = {
                "percentile": _percentile(
                    cohort["cohort_values"], current_value
                ),
                "cohort_size": cohort["cohort_size"],
                "radius_km": inputs.radius_km,
            }

        delta_absolute: float | None = None
        delta_pct: float | None = None
        if baseline_value is not None and current_value is not None:
            delta_absolute = round(current_value - baseline_value, 2)
            if baseline_value != 0:
                delta_pct = round(
                    100 * (current_value - baseline_value) / baseline_value,
                    1,
                )

        return {
            "metric": inputs.metric.value,
            "metric_label": _METRIC_LABELS_ES[inputs.metric],
            "comparison_kind": inputs.vs.value,
            "period": {
                "from": eff_from.isoformat(),
                "to": eff_to.isoformat(),
            },
            "current": {
                "value": current_value,
                "sample_size": current["count"],
            },
            "baseline": baseline_payload,
            "delta_absolute": delta_absolute,
            "delta_pct": delta_pct,
            **extras,
        }

    return ToolSpec(
        name="compare_to_baseline",
        description=(
            "Compares one metric (rating, review_count, sentiment_score, "
            "response_rate) for the restaurant in a given period against "
            "a chosen baseline: the prior equal-length period, the "
            "all-time history, or the geographic competition (percentile "
            "+ cohort size). Use this when the owner explicitly asks "
            "'how am I doing vs X?' — it returns one focused answer "
            "instead of forcing the LLM to compose multiple list/summarize "
            "calls. For a multi-metric panorama, prefer "
            "summarize_reviews_period instead."
        ),
        input_schema=pydantic_to_anthropic_schema(CompareToBaselineInput),
        handler=handler,
    )


# ──────────────────────────────────────────────────────────────────────────
#   Helpers for compare_to_baseline
# ──────────────────────────────────────────────────────────────────────────


def _extract_metric(
    aggregate: dict[str, Any], metric: BaselineMetric
) -> float | None:
    """Pull one scalar out of an ``_aggregate_period`` payload."""
    if metric is BaselineMetric.rating:
        return aggregate["rating_avg"]
    if metric is BaselineMetric.review_count:
        return float(aggregate["count"])
    if metric is BaselineMetric.sentiment_score:
        return aggregate["sentiment_avg"]
    if metric is BaselineMetric.response_rate:
        if aggregate["count"] == 0:
            return None
        return round(
            aggregate["responded_count"] / aggregate["count"], 3
        )
    return None


async def _competition_metric(
    db: AsyncSession,
    restaurant_id: str,
    metric: BaselineMetric,
    from_date: date,
    to_date: date,
    radius_km: float,
) -> dict[str, Any]:
    """Aggregate the metric across geographic peers and return the
    cohort distribution. Excludes the owner's restaurant itself.

    Restaurants that have zero reviews in the period are excluded —
    they would skew rating averages and don't represent a meaningful
    peer for response_rate either.
    """
    own = (
        await db.execute(
            select(Restaurant).where(Restaurant.id == restaurant_id)
        )
    ).scalars().first()
    if own is None or own.latitude is None or own.longitude is None:
        return {"cohort_size": 0, "cohort_avg": None, "cohort_values": []}

    own_lat = float(own.latitude)
    own_lng = float(own.longitude)

    candidates = list(
        (
            await db.execute(
                select(Restaurant.id, Restaurant.latitude, Restaurant.longitude)
                .where(
                    Restaurant.id != restaurant_id,
                    Restaurant.latitude.is_not(None),
                    Restaurant.longitude.is_not(None),
                )
            )
        ).all()
    )

    peer_ids: list[str] = []
    for cand_id, cand_lat, cand_lng in candidates:
        if cand_lat is None or cand_lng is None:
            continue
        distance = _haversine_km(
            own_lat, own_lng, float(cand_lat), float(cand_lng)
        )
        if distance <= radius_km:
            peer_ids.append(cand_id)

    if not peer_ids:
        return {"cohort_size": 0, "cohort_avg": None, "cohort_values": []}

    cohort_values: list[float] = []
    for peer_id in peer_ids:
        agg = await _aggregate_period(db, peer_id, from_date, to_date)
        if agg["count"] == 0:
            continue
        value = _extract_metric(agg, metric)
        if value is not None:
            cohort_values.append(value)

    if not cohort_values:
        return {"cohort_size": 0, "cohort_avg": None, "cohort_values": []}

    cohort_avg = round(sum(cohort_values) / len(cohort_values), 2)
    return {
        "cohort_size": len(cohort_values),
        "cohort_avg": cohort_avg,
        "cohort_values": cohort_values,
    }


# ──────────────────────────────────────────────────────────────────────────
#   update_owner_preferences
# ──────────────────────────────────────────────────────────────────────────


def make_update_owner_preferences_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    restaurant_scope_id: str | None,
) -> ToolSpec:
    """Tool que el agente Business llama cuando el owner pide algo
    persistente sobre el chat: tono fijo, idioma de respuesta, KPIs
    prioritarios. Una conversación nueva levanta este state vía el
    system prompt, así que el efecto es entre sesiones, no solo en
    el turno actual.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {
                "error": (
                    "update_owner_preferences requires an authenticated "
                    "owner. The current conversation is anonymous."
                )
            }
        if restaurant_scope_id is None:
            return {
                "error": (
                    "update_owner_preferences requires a restaurant "
                    "scope (this tool only applies to the Business "
                    "agent on a verified owner conversation)."
                )
            }

        try:
            inputs = UpdateOwnerPreferencesInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for update_owner_preferences.",
                "details": exc.errors(include_url=False),
            }

        if (
            inputs.tone is None
            and inputs.language is None
            and inputs.kpi_focus is None
        ):
            return {
                "error": (
                    "Pass at least one of tone, language, or kpi_focus. "
                    "If the owner didn't ask to change anything "
                    "persistent, don't call this tool."
                )
            }

        try:
            restaurant_uuid = uuid.UUID(restaurant_scope_id)
        except (TypeError, ValueError):
            return {"error": "Internal scope is not a valid UUID."}

        # The service treats ``None`` as "don't touch this field". We
        # only forward the fields the LLM actually populated, so each
        # call updates a partial set without overwriting the rest.
        prefs = await upsert_chat_preference(
            db,
            user_id=user_id,
            restaurant_id=restaurant_uuid,
            tone_preference=inputs.tone.value if inputs.tone else None,
            language_preference=(
                inputs.language.value if inputs.language else None
            ),
            kpi_focus=inputs.kpi_focus,
        )

        return {
            "saved": True,
            "preferences": {
                "tone": prefs.tone_preference,
                "language": prefs.language_preference,
                "kpi_focus": prefs.kpi_focus,
            },
            "note": (
                "La preferencia ya está guardada. Aplica desde la "
                "PRÓXIMA sesión también; en ESTE turno seguís usando el "
                "tono/idioma vigente al inicio de la conversación. "
                "Confirmá al owner en una frase corta y seguí con su "
                "pregunta original si la había."
            ),
        }

    return ToolSpec(
        name="update_owner_preferences",
        description=(
            "Persists owner preferences for the chat (tone, language, "
            "KPI focus) so future sessions remember them. Call this "
            "ONLY when the owner explicitly says something persistent "
            "(e.g. 'always reply in Portuguese', 'siempre tono formal', "
            "'mostrame siempre la tasa de respuesta'). Do NOT call it "
            "when they make a one-off request — for that, just answer "
            "in the current turn. Pass only the fields the owner "
            "mentioned; existing preferences for other fields stay. "
            "To replace a preference, set it to a new explicit value."
        ),
        input_schema=pydantic_to_anthropic_schema(UpdateOwnerPreferencesInput),
        handler=handler,
    )
