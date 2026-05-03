"""Unit tests for the sentiment service.

Covers the pure helpers (clamping, label/score reconciliation), the
graceful-degradation path when ``GEMINI_API_KEY`` is unset, and a
happy-path call with the HTTP layer mocked.

These never touch the database, so they don't require ``RUN_INTEGRATION``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.dish import SentimentLabel
from app.services import sentiment_service
from app.services.sentiment_service import (
    SentimentResult,
    _clamp_score,
    _coerce_label,
    analyze_review_text,
)


def test_clamp_score_clips_above_one():
    assert _clamp_score(1.5) == 1.0


def test_clamp_score_clips_below_minus_one():
    assert _clamp_score(-2.0) == -1.0


def test_clamp_score_rounds_to_two_decimals():
    assert _clamp_score(0.4567) == 0.46


def test_coerce_label_passes_through_when_consistent():
    assert _coerce_label("positive", 0.7) == SentimentLabel.positive
    assert _coerce_label("negative", -0.6) == SentimentLabel.negative
    assert _coerce_label("neutral", 0.05) == SentimentLabel.neutral


def test_coerce_label_flips_positive_to_negative_when_score_is_very_negative():
    # Truncated outputs sometimes flip the label keyword while keeping
    # the right score — we trust the score in that case.
    assert _coerce_label("positive", -0.8) == SentimentLabel.negative


def test_coerce_label_flips_negative_to_positive_when_score_is_very_positive():
    assert _coerce_label("negative", 0.9) == SentimentLabel.positive


def test_coerce_label_falls_back_to_neutral_for_unknown_string():
    assert _coerce_label("happy", 0.0) == SentimentLabel.neutral


@pytest.mark.asyncio
async def test_analyze_review_text_returns_none_without_api_key():
    with patch.object(sentiment_service.settings, "GEMINI_API_KEY", None):
        result = await analyze_review_text("Excelente plato", rating=5.0)
    assert result is None


@pytest.mark.asyncio
async def test_analyze_review_text_returns_none_for_empty_text():
    with patch.object(sentiment_service.settings, "GEMINI_API_KEY", "fake-key"):
        result = await analyze_review_text("   ", rating=4.0)
    assert result is None


@pytest.mark.asyncio
async def test_analyze_review_text_happy_path_negative():
    """Mock httpx so the real Gemini endpoint isn't called. Returns the
    JSON payload our service parses and normalises."""
    fake_payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {"label": "negative", "score": -0.8}
                            )
                        }
                    ]
                }
            }
        ]
    }

    response_mock = MagicMock()
    response_mock.raise_for_status = MagicMock(return_value=None)
    response_mock.json = MagicMock(return_value=fake_payload)

    client_mock = MagicMock()
    client_mock.post = AsyncMock(return_value=response_mock)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(sentiment_service.settings, "GEMINI_API_KEY", "fake-key"),
        patch("app.services.sentiment_service.httpx.AsyncClient", return_value=client_mock),
    ):
        result = await analyze_review_text(
            "El plato llegó frío y la atención fue pésima.", rating=2.0
        )

    assert isinstance(result, SentimentResult)
    assert result.label == SentimentLabel.negative
    assert result.score == -0.8


@pytest.mark.asyncio
async def test_analyze_review_text_returns_none_on_unparseable_payload():
    fake_payload = {
        "candidates": [
            {"content": {"parts": [{"text": "this is not json {{{"}]}}
        ]
    }
    response_mock = MagicMock()
    response_mock.raise_for_status = MagicMock(return_value=None)
    response_mock.json = MagicMock(return_value=fake_payload)

    client_mock = MagicMock()
    client_mock.post = AsyncMock(return_value=response_mock)
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(sentiment_service.settings, "GEMINI_API_KEY", "fake-key"),
        patch("app.services.sentiment_service.httpx.AsyncClient", return_value=client_mock),
    ):
        result = await analyze_review_text("texto", rating=3.0)

    assert result is None


def test_dish_review_response_does_not_expose_sentiment():
    """Privacy guard: the public review schema must not surface the
    sentiment fields. Owner-only data leaking on a public endpoint would
    let anyone scrape sentiment per author."""
    from app.schemas.dish import DishReviewResponse

    fields = set(DishReviewResponse.model_fields.keys())
    assert "sentiment_label" not in fields
    assert "sentiment_score" not in fields
    assert "sentiment_analyzed_at" not in fields
