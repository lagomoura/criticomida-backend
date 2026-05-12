"""Unit tests for the vision service.

Covers graceful degradation, the ``_normalize_output`` helper (which
encodes most of our defensive cleanup), and a happy-path call with the
SDK mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import vision_service
from app.services.vision_service import (
    _VisionSchema,
    _normalize_output,
    analyze_dish_photo,
)


def test_normalize_output_lowercases_dedupes_and_strips_hash_tags():
    raw = _VisionSchema(
        tags=["#Crocante", "crocante", "DORADO", " "],
        visible_ingredients=["Arroz", " AZAFRÁN ", ""],
        plating_style="Minimalist",
        editorial_blurb="  Una porción generosa. Salsa abundante.  ",
        suggested_pros=["queso bien fundido", ""],
        suggested_cons=["presentación apretada"],
    )
    out = _normalize_output(raw)
    assert out["tags"] == ["crocante", "dorado"]
    assert out["visible_ingredients"] == ["arroz", "azafrán"]
    assert out["plating_style"] == "minimalist"
    assert out["editorial_blurb"].startswith("Una porción generosa.")
    assert out["suggested_pros"] == ["queso bien fundido"]
    assert out["suggested_cons"] == ["presentación apretada"]


def test_normalize_output_drops_unknown_plating_style():
    raw = _VisionSchema(
        tags=["crocante", "dorado", "porcion"],
        visible_ingredients=["queso"],
        plating_style="hipster",
        editorial_blurb="Blurb.",
        suggested_pros=["pro"],
        suggested_cons=[],
    )
    out = _normalize_output(raw)
    assert out["plating_style"] is None


def test_normalize_output_blank_blurb_becomes_none():
    raw = _VisionSchema(
        tags=["a", "b", "c"],
        visible_ingredients=[],
        plating_style="rustic",
        editorial_blurb="   ",
        suggested_pros=["pro"],
        suggested_cons=[],
    )
    out = _normalize_output(raw)
    assert out["editorial_blurb"] is None


@pytest.mark.asyncio
async def test_analyze_dish_photo_returns_empty_without_api_key():
    with patch.object(vision_service.settings, "GEMINI_API_KEY", None):
        vision_service._client = None
        out = await analyze_dish_photo(photo_bytes=b"\xff\xd8fake", photo_mime="image/jpeg")
    assert out == vision_service._empty_response()


@pytest.mark.asyncio
async def test_analyze_dish_photo_returns_empty_when_no_bytes_or_url():
    fake_client = MagicMock()
    with (
        patch.object(vision_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(vision_service, "_client", fake_client),
    ):
        out = await analyze_dish_photo()
    assert out == vision_service._empty_response()


@pytest.mark.asyncio
async def test_analyze_dish_photo_happy_path():
    parsed = _VisionSchema(
        tags=["crocante", "dorado", "porción"],
        visible_ingredients=["queso", "tomate"],
        plating_style="rustic",
        editorial_blurb="Porción generosa, masa bien aireada. Queso fundido en bordes.",
        suggested_pros=["masa aireada", "queso bien fundido"],
        suggested_cons=["presentación apretada"],
    )
    fake_response = MagicMock()
    fake_response.parsed = parsed
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(vision_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(vision_service, "_client", fake_client),
    ):
        out = await analyze_dish_photo(
            photo_bytes=b"\xff\xd8fake-jpeg", photo_mime="image/jpeg"
        )

    assert out["tags"] == ["crocante", "dorado", "porción"]
    assert out["visible_ingredients"] == ["queso", "tomate"]
    assert out["plating_style"] == "rustic"
    assert out["editorial_blurb"].startswith("Porción generosa")
    assert out["suggested_pros"] == ["masa aireada", "queso bien fundido"]


@pytest.mark.asyncio
async def test_analyze_dish_photo_returns_empty_on_unparseable_response():
    fake_response = MagicMock()
    fake_response.parsed = None
    fake_response.candidates = []
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(vision_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(vision_service, "_client", fake_client),
    ):
        out = await analyze_dish_photo(photo_bytes=b"\xff\xd8x")

    assert out == vision_service._empty_response()
