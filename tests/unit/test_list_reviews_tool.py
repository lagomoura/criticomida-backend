"""Unit tests for the ``list_reviews`` tool contract.

The contract is enforced by Pydantic, so these tests don't need a DB.
We hit the handler with bad payloads and verify it surfaces a structured
error the agent loop can hand back to the model. Happy-path queries
against real data are covered by the eval suite (Phase 1 of the
chatbot quality work).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.chat.tools.business import make_list_reviews_tool


@pytest.fixture
def tool():
    return make_list_reviews_tool(AsyncMock(), restaurant_scope_id="r1")


@pytest.fixture
def tool_no_scope():
    return make_list_reviews_tool(AsyncMock(), restaurant_scope_id=None)


class TestSchemaShape:
    def test_responded_status_is_strict_enum(self, tool):
        prop = tool.input_schema["properties"]["responded_status"]
        assert prop["enum"] == ["any", "pending", "responded"]
        assert prop["type"] == "string"

    def test_sentiment_is_strict_enum(self, tool):
        prop = tool.input_schema["properties"]["sentiment"]
        assert prop["enum"] == ["any", "positive", "neutral", "negative"]

    def test_sort_enum_includes_all_orders(self, tool):
        prop = tool.input_schema["properties"]["sort"]
        assert set(prop["enum"]) == {
            "recent",
            "oldest",
            "rating_high",
            "rating_low",
            "most_negative",
            "most_positive",
        }

    def test_extra_properties_are_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False

    def test_descriptions_carry_no_synonym_hints(self, tool):
        # Regression: descriptions used to list multilingual synonyms
        # (``"también: unanswered, sin_responder"``). Synonyms moved
        # into the LLM, not into the tool contract.
        leaked_terms = (
            "también:",
            "synonym",
            "newest_first",
            "sin_responder",
            "harshest",
            "mas_duras",
        )
        for prop in tool.input_schema["properties"].values():
            desc = prop.get("description", "")
            for hint in leaked_terms:
                assert hint not in desc, (
                    f"property description leaked synonym hint {hint!r}: {desc!r}"
                )

    def test_limit_bounds_are_one_to_fifty(self, tool):
        prop = tool.input_schema["properties"]["limit"]
        assert prop["minimum"] == 1
        assert prop["maximum"] == 50


class TestHandlerErrors:
    async def test_missing_scope_returns_clean_error(self, tool_no_scope):
        result = await tool_no_scope.handler({})
        assert result == {"error": "Business scope is required."}
        assert "details" not in result
        assert "notes" not in result

    async def test_invalid_responded_status_surfaces_validation_error(self, tool):
        result = await tool.handler({"responded_status": "no"})
        assert "error" in result
        assert "details" in result
        # Enough information for the LLM to recover.
        details = result["details"]
        assert any(d["loc"] == ("responded_status",) or d["loc"] == ["responded_status"] for d in details)
        assert any(
            "any" in d["msg"] and "pending" in d["msg"] and "responded" in d["msg"]
            for d in details
        )

    @pytest.mark.parametrize(
        "value",
        [
            "no",            # the bug case from production
            "sí",
            "yes",
            "true",
            "false",
            "ainda_nao",
            "TODAVIA NO",
        ],
    )
    async def test_yes_no_style_values_are_rejected_in_any_language(
        self, tool, value
    ):
        # Old behaviour: silent fallback to 'any', then leak via ``notes``.
        # New behaviour: explicit error, agent loop reintents.
        result = await tool.handler({"responded_status": value})
        assert "error" in result, f"value {value!r} should have been rejected"

    async def test_out_of_range_min_rating_rejected(self, tool):
        result = await tool.handler({"min_rating": 10})
        assert "error" in result
        assert any(
            "min_rating" in (d["loc"] if isinstance(d["loc"], list) else list(d["loc"]))
            for d in result["details"]
        )

    async def test_out_of_range_limit_rejected(self, tool):
        result = await tool.handler({"limit": 999})
        assert "error" in result

    async def test_extra_param_is_rejected(self, tool):
        result = await tool.handler({"limit": 5, "ramdom_field": "x"})
        assert "error" in result
        details = result["details"]
        assert any(
            (d["loc"][0] if isinstance(d["loc"], (list, tuple)) else d["loc"])
            == "ramdom_field"
            for d in details
        )

    async def test_invalid_date_format_rejected(self, tool):
        result = await tool.handler({"date_from": "not-a-date"})
        assert "error" in result

    async def test_invalid_sentiment_rejected(self, tool):
        result = await tool.handler({"sentiment": "mixed"})
        assert "error" in result

    async def test_invalid_sort_rejected(self, tool):
        result = await tool.handler({"sort": "newest"})
        assert "error" in result

    async def test_response_never_carries_notes_field(self, tool):
        # Regression: the legacy tool used a ``notes`` array to leak
        # fallback explanations to the LLM, which then leaked them to
        # the owner. Errors surface via ``error`` only now.
        result = await tool.handler({"responded_status": "no"})
        assert "notes" not in result
