"""Unit tests for the ``summarize_reviews_period`` tool contract.

Like ``test_list_reviews_tool``, this only exercises the validation /
error surface — the SQL aggregation logic is covered indirectly by
the eval suite (which runs against a real DB fixture).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.chat.tools.insights import (
    make_summarize_reviews_period_tool,
)


@pytest.fixture
def tool():
    return make_summarize_reviews_period_tool(
        AsyncMock(), restaurant_scope_id="r1"
    )


@pytest.fixture
def tool_no_scope():
    return make_summarize_reviews_period_tool(
        AsyncMock(), restaurant_scope_id=None
    )


class TestSchemaShape:
    def test_dimensions_is_strict_enum(self, tool):
        prop = tool.input_schema["properties"]["dimensions"]
        assert prop["type"] == "array"
        assert prop["items"]["enum"] == ["sentiment", "rating", "responded"]

    def test_extra_properties_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False

    def test_required_dates(self, tool):
        assert set(tool.input_schema["required"]) == {"from_date", "to_date"}


class TestHandlerErrors:
    async def test_missing_scope_returns_clean_error(self, tool_no_scope):
        result = await tool_no_scope.handler(
            {"from_date": "2026-04-01", "to_date": "2026-04-30"}
        )
        assert result == {"error": "Business scope is required."}

    async def test_invalid_dimension_rejected(self, tool):
        result = await tool.handler(
            {
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
                "dimensions": ["themes"],  # not in enum (yet)
            }
        )
        assert "error" in result
        assert "details" in result

    async def test_uppercase_dimension_accepted(self, tool):
        # Validation must accept uppercase enum values (case-insensitive
        # field validator). We can't run the SQL with an AsyncMock, so
        # we accept any post-validation failure mode — the only thing
        # this test cares about is that we *passed* validation.
        try:
            result = await tool.handler(
                {
                    "from_date": "2026-04-01",
                    "to_date": "2026-04-30",
                    "dimensions": ["SENTIMENT", "RATING"],
                }
            )
            if isinstance(result, dict) and "error" in result:
                assert "Invalid arguments" not in result.get("error", "")
        except (AttributeError, TypeError):
            # AsyncMock returned a non-iterable for db.execute — proves
            # the validator already passed.
            pass

    async def test_inverted_date_range_returns_error(self, tool):
        result = await tool.handler(
            {"from_date": "2026-04-30", "to_date": "2026-04-01"}
        )
        assert "error" in result
        assert "from_date" in result["error"].lower()

    async def test_missing_from_date_rejected(self, tool):
        result = await tool.handler({"to_date": "2026-04-30"})
        assert "error" in result
        assert "details" in result

    async def test_extra_param_rejected(self, tool):
        result = await tool.handler(
            {
                "from_date": "2026-04-01",
                "to_date": "2026-04-30",
                "include_themes": True,  # not in schema
            }
        )
        assert "error" in result

    async def test_invalid_date_format_rejected(self, tool):
        result = await tool.handler(
            {"from_date": "not-a-date", "to_date": "2026-04-30"}
        )
        assert "error" in result
