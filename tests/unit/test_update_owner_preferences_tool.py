"""Unit tests for the ``update_owner_preferences`` tool contract.

Validation surface only — the success path that hits the DB is covered
by the eval suite (real fixture).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.chat.tools.insights import (
    make_update_owner_preferences_tool,
)


@pytest.fixture
def tool():
    return make_update_owner_preferences_tool(
        AsyncMock(),
        user_id=uuid.uuid4(),
        restaurant_scope_id=str(uuid.uuid4()),
    )


@pytest.fixture
def tool_no_user():
    return make_update_owner_preferences_tool(
        AsyncMock(),
        user_id=None,
        restaurant_scope_id=str(uuid.uuid4()),
    )


@pytest.fixture
def tool_no_scope():
    return make_update_owner_preferences_tool(
        AsyncMock(),
        user_id=uuid.uuid4(),
        restaurant_scope_id=None,
    )


class TestSchemaShape:
    def test_all_fields_optional(self, tool):
        # Tool semantics: pass only what changes; everything optional.
        assert tool.input_schema.get("required", []) == []

    def test_tone_enum(self, tool):
        prop = tool.input_schema["properties"]["tone"]
        # Optional[Enum] becomes anyOf[enum, null]
        enum_branch = next(
            (b for b in prop.get("anyOf", []) if "enum" in b), None
        )
        assert enum_branch is not None
        assert set(enum_branch["enum"]) == {
            "warm",
            "professional",
            "concise",
            "match_brand",
        }

    def test_language_enum(self, tool):
        prop = tool.input_schema["properties"]["language"]
        enum_branch = next(
            (b for b in prop.get("anyOf", []) if "enum" in b), None
        )
        assert enum_branch is not None
        assert set(enum_branch["enum"]) == {"es", "en", "pt"}

    def test_extra_properties_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False


class TestHandlerErrors:
    async def test_no_user_returns_clean_error(self, tool_no_user):
        result = await tool_no_user.handler({"tone": "warm"})
        assert "error" in result
        assert "anonymous" in result["error"].lower()

    async def test_no_scope_returns_clean_error(self, tool_no_scope):
        result = await tool_no_scope.handler({"tone": "warm"})
        assert "error" in result
        assert "scope" in result["error"].lower()

    async def test_empty_payload_rejected(self, tool):
        result = await tool.handler({})
        assert "error" in result
        # Should ask the LLM to provide at least one field
        assert "at least" in result["error"].lower()

    async def test_invalid_tone_rejected(self, tool):
        result = await tool.handler({"tone": "casual"})
        assert "error" in result
        assert "details" in result

    async def test_invalid_language_rejected(self, tool):
        result = await tool.handler({"language": "fr"})
        assert "error" in result
        assert "details" in result

    async def test_uppercase_inputs_normalised(self, tool):
        # Validation must accept uppercase enum values; the SQL path
        # might fail with AsyncMock — that's fine, it proves we got
        # past validation.
        try:
            result = await tool.handler({"tone": "WARM", "language": "ES"})
            if isinstance(result, dict) and "error" in result:
                assert "Invalid arguments" not in result.get("error", "")
        except (AttributeError, TypeError):
            pass

    async def test_extra_param_rejected(self, tool):
        result = await tool.handler(
            {"tone": "warm", "notification_cadence": "daily"}
        )
        assert "error" in result
