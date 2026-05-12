"""Unit tests for the chat-title service.

Covers graceful degradation when ``GEMINI_API_KEY`` is unset, the
title-cleanup helper, and a happy-path call with the SDK mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import chat_title_service
from app.services.chat_title_service import (
    _TitleSchema,
    _clean_title,
    generate_conversation_title,
)


def test_clean_title_collapses_whitespace_and_strips_quotes():
    assert _clean_title('  "Caída del   rating"  ') == "Caída del rating"


def test_clean_title_returns_none_for_blank_input():
    assert _clean_title("    ") is None


def test_clean_title_truncates_to_max_length():
    long = "palabra " * 30
    result = _clean_title(long)
    assert result is not None
    assert len(result) <= 80
    assert result.endswith("…")


@pytest.mark.asyncio
async def test_generate_conversation_title_returns_none_without_api_key():
    with patch.object(chat_title_service.settings, "GEMINI_API_KEY", None):
        chat_title_service._client = None
        result = await generate_conversation_title([("user", "Hola")])
    assert result is None


@pytest.mark.asyncio
async def test_generate_conversation_title_returns_none_for_empty_messages():
    fake_client = MagicMock()
    with (
        patch.object(chat_title_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(chat_title_service, "_client", fake_client),
    ):
        result = await generate_conversation_title([])
    assert result is None


@pytest.mark.asyncio
async def test_generate_conversation_title_happy_path():
    fake_response = MagicMock()
    fake_response.parsed = _TitleSchema(title="Caída del rating mensual")
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(chat_title_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(chat_title_service, "_client", fake_client),
    ):
        result = await generate_conversation_title(
            [
                ("user", "¿Por qué bajó mi rating?"),
                ("assistant", "Te falta cobertura de reseñas en el mes."),
            ]
        )

    assert result == "Caída del rating mensual"


@pytest.mark.asyncio
async def test_generate_conversation_title_returns_none_on_unparseable_payload():
    fake_response = MagicMock()
    fake_response.parsed = None
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(chat_title_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(chat_title_service, "_client", fake_client),
    ):
        result = await generate_conversation_title([("user", "x")])

    assert result is None
