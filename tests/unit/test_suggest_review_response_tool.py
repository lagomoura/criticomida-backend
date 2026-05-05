"""Unit tests for the ``suggest_review_response`` tool contract.

These cover validation and the error surface — the success path
(which loads a real review from the DB) is exercised by the eval
suite where the fixture provides reviews with known IDs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.chat.tools.insights import (
    make_suggest_review_response_tool,
)


import uuid as _uuid


@pytest.fixture
def tool():
    return make_suggest_review_response_tool(
        AsyncMock(),
        user_id=_uuid.uuid4(),
        restaurant_scope_id="r1",
    )


@pytest.fixture
def tool_no_scope():
    return make_suggest_review_response_tool(
        AsyncMock(),
        user_id=_uuid.uuid4(),
        restaurant_scope_id=None,
    )


class TestSchemaShape:
    def test_tone_is_strict_enum(self, tool):
        # Tone is Optional[ResponseTone] — Pydantic emits anyOf[enum, null].
        prop = tool.input_schema["properties"]["tone"]
        enum_branch = next(
            (b for b in prop.get("anyOf", []) if "enum" in b),
            None,
        )
        assert enum_branch is not None, prop
        assert enum_branch["enum"] == [
            "warm",
            "professional",
            "apologetic",
            "match_brand",
        ]

    def test_review_id_is_required(self, tool):
        assert "review_id" in tool.input_schema.get("required", [])

    def test_extra_properties_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False


class TestHandlerErrors:
    async def test_missing_scope_returns_clean_error(self, tool_no_scope):
        result = await tool_no_scope.handler(
            {
                "review_id": "00000000-0000-0000-0000-000000000000",
                "tone": "professional",
            }
        )
        assert result == {"error": "Business scope is required."}

    async def test_missing_tone_is_accepted_and_inferred(self, tool):
        # Tone is optional — the handler infers from review sentiment
        # when omitted. Validation must pass; the actual inference
        # depends on a real review and is exercised by the eval suite.
        try:
            result = await tool.handler(
                {"review_id": "00000000-0000-0000-0000-000000000000"}
            )
            if isinstance(result, dict) and "error" in result:
                assert "Invalid arguments" not in result.get("error", "")
        except (AttributeError, TypeError):
            pass  # AsyncMock can't satisfy the SQL path

    async def test_invalid_tone_rejected(self, tool):
        result = await tool.handler(
            {
                "review_id": "00000000-0000-0000-0000-000000000000",
                "tone": "casual",  # not in enum
            }
        )
        assert "error" in result
        assert "details" in result

    async def test_uppercase_tone_accepted_by_validator(self, tool):
        # Validation must accept uppercase enum values (case-insensitive
        # field validator). We accept any post-validation failure mode
        # because AsyncMock can't satisfy the SQL path.
        try:
            result = await tool.handler(
                {
                    "review_id": "00000000-0000-0000-0000-000000000000",
                    "tone": "WARM",
                }
            )
            if isinstance(result, dict) and "error" in result:
                # Any error here should NOT be the validation one.
                assert "Invalid arguments" not in result.get("error", "")
        except (AttributeError, TypeError):
            # Mocked DB path crashed — proves we got past validation.
            pass

    async def test_missing_review_id_rejected(self, tool):
        result = await tool.handler({"tone": "warm"})
        assert "error" in result
        assert "details" in result

    async def test_extra_param_rejected(self, tool):
        result = await tool.handler(
            {
                "review_id": "00000000-0000-0000-0000-000000000000",
                "tone": "warm",
                "max_length": 200,  # not in schema
            }
        )
        assert "error" in result

    async def test_non_uuid_review_id_returns_clean_error(self, tool):
        # Passes Pydantic (string), fails at the UUID parse inside the
        # handler — should yield a clean error, not a stack trace.
        result = await tool.handler(
            {"review_id": "not-a-uuid", "tone": "warm"}
        )
        assert "error" in result
        assert "uuid" in result["error"].lower()
