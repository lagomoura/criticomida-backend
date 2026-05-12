"""Unit tests for the embeddings service.

Covers the graceful-degradation path when ``GEMINI_API_KEY`` is unset,
and a happy-path call with the SDK layer mocked. These never touch the
database, so they don't require ``RUN_INTEGRATION``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import embeddings_service
from app.services.embeddings_service import (
    EMBEDDING_DIMENSIONS,
    embed_documents,
    embed_image,
    embed_query,
)


def _fake_embedding(dim: int = EMBEDDING_DIMENSIONS) -> MagicMock:
    emb = MagicMock()
    # Already L2-normalized (1.0 on the first axis); ``_normalize_vector``
    # is a no-op on this shape.
    emb.values = [1.0] + [0.0] * (dim - 1)
    return emb


@pytest.mark.asyncio
async def test_embed_query_returns_none_without_api_key():
    with patch.object(embeddings_service.settings, "GEMINI_API_KEY", None):
        embeddings_service._client = None
        result = await embed_query("pizza al horno de leña")
    assert result is None


@pytest.mark.asyncio
async def test_embed_query_returns_none_for_blank_text():
    fake_client = MagicMock()
    with (
        patch.object(embeddings_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(embeddings_service, "_client", fake_client),
    ):
        result = await embed_query("   ")
    assert result is None


@pytest.mark.asyncio
async def test_embed_query_happy_path():
    fake_response = MagicMock()
    fake_response.embeddings = [_fake_embedding()]
    fake_client = MagicMock()
    fake_client.aio.models.embed_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(embeddings_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(embeddings_service, "_client", fake_client),
    ):
        result = await embed_query("fugazzeta crocante")

    assert result is not None
    assert len(result) == EMBEDDING_DIMENSIONS
    assert result[0] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_embed_documents_returns_nones_without_api_key():
    with patch.object(embeddings_service.settings, "GEMINI_API_KEY", None):
        embeddings_service._client = None
        result = await embed_documents(["a", "b", "c"])
    assert result == [None, None, None]


@pytest.mark.asyncio
async def test_embed_documents_happy_path_returns_one_vector_per_input():
    fake_response = MagicMock()
    fake_response.embeddings = [_fake_embedding(), _fake_embedding()]
    fake_client = MagicMock()
    fake_client.aio.models.embed_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(embeddings_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(embeddings_service, "_client", fake_client),
    ):
        result = await embed_documents(["uno", "dos"])

    assert len(result) == 2
    assert all(v is not None and len(v) == EMBEDDING_DIMENSIONS for v in result)


@pytest.mark.asyncio
async def test_embed_documents_pads_with_none_when_api_skips_entries():
    """Defensive: if Gemini returns fewer embeddings than we asked for,
    we must still keep the input/output positions aligned."""
    fake_response = MagicMock()
    fake_response.embeddings = [_fake_embedding()]  # one short
    fake_client = MagicMock()
    fake_client.aio.models.embed_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(embeddings_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(embeddings_service, "_client", fake_client),
    ):
        result = await embed_documents(["uno", "dos"])

    assert len(result) == 2
    assert result[0] is not None
    assert result[1] is None


@pytest.mark.asyncio
async def test_embed_image_returns_none_without_api_key():
    with patch.object(embeddings_service.settings, "GEMINI_API_KEY", None):
        embeddings_service._client = None
        result = await embed_image(b"\xff\xd8\xff\xe0fake-jpeg")
    assert result is None


@pytest.mark.asyncio
async def test_embed_image_returns_none_for_empty_bytes():
    fake_client = MagicMock()
    with (
        patch.object(embeddings_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(embeddings_service, "_client", fake_client),
    ):
        result = await embed_image(b"")
    assert result is None


@pytest.mark.asyncio
async def test_embed_image_happy_path():
    fake_response = MagicMock()
    fake_response.embeddings = [_fake_embedding()]
    fake_client = MagicMock()
    fake_client.aio.models.embed_content = AsyncMock(return_value=fake_response)

    with (
        patch.object(embeddings_service.settings, "GEMINI_API_KEY", "k"),
        patch.object(embeddings_service, "_client", fake_client),
    ):
        result = await embed_image(b"\xff\xd8\xff\xe0fake-jpeg")

    assert result is not None
    assert len(result) == EMBEDDING_DIMENSIONS
