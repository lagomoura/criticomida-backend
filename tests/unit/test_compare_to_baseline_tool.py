"""Unit tests for the ``compare_to_baseline`` tool contract.

Validation surface and clear error paths. The success path that does
geographic clustering + percentiles is exercised by the eval suite
(real DB fixture).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.chat.tools.insights import (
    make_compare_to_baseline_tool,
)


@pytest.fixture
def tool():
    return make_compare_to_baseline_tool(
        AsyncMock(), restaurant_scope_id="r1"
    )


@pytest.fixture
def tool_no_scope():
    return make_compare_to_baseline_tool(
        AsyncMock(), restaurant_scope_id=None
    )


class TestSchemaShape:
    def test_metric_is_strict_enum(self, tool):
        prop = tool.input_schema["properties"]["metric"]
        assert prop["enum"] == [
            "rating",
            "review_count",
            "sentiment_score",
            "response_rate",
        ]

    def test_vs_is_strict_enum(self, tool):
        prop = tool.input_schema["properties"]["vs"]
        assert prop["enum"] == ["prior_period", "all_time", "competition"]

    def test_required_fields(self, tool):
        # Only metric and vs are required — dates default to last 30
        # days so the LLM doesn't have to guess them when the owner
        # asks "compared to competition" without specifying a window.
        assert set(tool.input_schema["required"]) == {"metric", "vs"}

    def test_radius_km_is_optional_with_default(self, tool):
        prop = tool.input_schema["properties"]["radius_km"]
        assert prop["minimum"] == 0.5
        assert prop["maximum"] == 20.0
        assert prop["default"] == 2.0

    def test_extra_properties_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False


class TestHandlerErrors:
    async def test_missing_scope_returns_clean_error(self, tool_no_scope):
        result = await tool_no_scope.handler(
            {
                "metric": "rating",
                "vs": "prior_period",
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
            }
        )
        assert result == {"error": "Business scope is required."}

    async def test_invalid_metric_rejected(self, tool):
        result = await tool.handler(
            {
                "metric": "stars",  # not in enum
                "vs": "prior_period",
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
            }
        )
        assert "error" in result
        assert "details" in result

    async def test_invalid_vs_rejected(self, tool):
        result = await tool.handler(
            {
                "metric": "rating",
                "vs": "everyone",  # not in enum
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
            }
        )
        assert "error" in result

    async def test_uppercase_enum_accepted(self, tool):
        try:
            result = await tool.handler(
                {
                    "metric": "RATING",
                    "vs": "PRIOR_PERIOD",
                    "from_date": "2026-04-01",
                    "to_date": "2026-04-30",
                }
            )
            if isinstance(result, dict) and "error" in result:
                assert "Invalid arguments" not in result.get("error", "")
        except (AttributeError, TypeError):
            pass  # AsyncMock cannot satisfy the SQL path

    async def test_inverted_dates_returns_error(self, tool):
        result = await tool.handler(
            {
                "metric": "rating",
                "vs": "prior_period",
                "from_date": "2026-04-30",
                "to_date": "2026-04-01",
            }
        )
        assert "error" in result
        assert "from_date" in result["error"].lower()

    async def test_competition_rejects_sentiment_score(self, tool):
        result = await tool.handler(
            {
                "metric": "sentiment_score",
                "vs": "competition",
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
            }
        )
        assert "error" in result
        assert "competition" in result["error"].lower()
        assert "sentiment_score" in result["error"]

    async def test_radius_out_of_range_rejected(self, tool):
        result = await tool.handler(
            {
                "metric": "rating",
                "vs": "competition",
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
                "radius_km": 50,  # > 20 max
            }
        )
        assert "error" in result

    async def test_extra_param_rejected(self, tool):
        result = await tool.handler(
            {
                "metric": "rating",
                "vs": "prior_period",
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
                "fake_param": True,
            }
        )
        assert "error" in result
