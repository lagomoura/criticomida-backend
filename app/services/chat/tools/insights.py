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

from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import Dish, DishReview
from app.models.owner_content import DishReviewOwnerResponse
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._schemas import (
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
