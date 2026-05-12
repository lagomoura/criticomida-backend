"""Unit tests for the Gemini Cached Content layer in ``agent_loop``.

The interesting invariants are:

1. The cache key is stable across calls with the same (model, system,
   tools) — that's what powers the hit-on-second-call savings.
2. Small prefixes skip caching entirely so we don't fire a doomed
   ``caches.create`` against Gemini's minimum-size guard.
3. ``caches.create`` failures degrade silently to ``None`` — the
   caller's inline path must still work after a cache miss.
4. The kill switch (``AGENT_LOOP_CACHE_DISABLED=1``) wins over
   everything.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from google.genai import types as genai_types

from app.services.chat import agent_loop as al


# Large enough to pass ``_CACHE_MIN_PREFIX_CHARS`` (4000).
_LONG_SYSTEM = "x" * 5000


def _dummy_tool(name: str = "ping") -> genai_types.Tool:
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name=name,
                description="d" * 200,
                parameters_json_schema={"type": "object", "properties": {}},
            )
        ]
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    al._clear_cached_content_registry()
    yield
    al._clear_cached_content_registry()


# --- _cache_key -----------------------------------------------------------


def test_cache_key_is_stable_for_same_inputs():
    tools = [_dummy_tool()]
    k1 = al._cache_key("m", "sys", tools)
    k2 = al._cache_key("m", "sys", tools)
    assert k1 == k2


def test_cache_key_changes_when_system_changes():
    tools = [_dummy_tool()]
    assert al._cache_key("m", "sys-a", tools) != al._cache_key("m", "sys-b", tools)


def test_cache_key_changes_when_tools_change():
    assert (
        al._cache_key("m", "sys", [_dummy_tool("a")])
        != al._cache_key("m", "sys", [_dummy_tool("b")])
    )


# --- _ensure_cached_content -----------------------------------------------


@pytest.mark.asyncio
async def test_ensure_cached_content_skips_when_below_min_size():
    """A small prefix must not even attempt ``caches.create``. The size
    check is local so we never fire a doomed request at the API."""
    fake_client = MagicMock()
    fake_client.aio.caches.create = AsyncMock()

    name = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system="tiny",
        tool_list=[],
    )
    assert name is None
    fake_client.aio.caches.create.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_cached_content_creates_on_first_call():
    fake_client = MagicMock()
    fake_client.aio.caches.create = AsyncMock(
        return_value=MagicMock(name="cachedContents/abc123")
    )
    # ``MagicMock(name=...)`` sets the mock's display name, not the
    # ``.name`` attribute. Set explicitly.
    fake_client.aio.caches.create.return_value.name = "cachedContents/abc123"

    name = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system=_LONG_SYSTEM,
        tool_list=[_dummy_tool()],
    )
    assert name == "cachedContents/abc123"
    fake_client.aio.caches.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_cached_content_reuses_on_second_call():
    """A second call with identical inputs must not hit the API again."""
    fake_client = MagicMock()
    fake_result = MagicMock()
    fake_result.name = "cachedContents/abc123"
    fake_client.aio.caches.create = AsyncMock(return_value=fake_result)

    name1 = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system=_LONG_SYSTEM,
        tool_list=[_dummy_tool()],
    )
    name2 = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system=_LONG_SYSTEM,
        tool_list=[_dummy_tool()],
    )

    assert name1 == name2 == "cachedContents/abc123"
    fake_client.aio.caches.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_cached_content_falls_back_to_none_on_error():
    """When ``caches.create`` raises, we log + return ``None`` so the
    caller falls back to inline. The registry must not retain the
    failed key — the next request gets a fresh attempt."""
    fake_client = MagicMock()
    fake_client.aio.caches.create = AsyncMock(
        side_effect=RuntimeError("below minimum")
    )

    name = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system=_LONG_SYSTEM,
        tool_list=[_dummy_tool()],
    )
    assert name is None
    # Nothing stored — the next attempt re-tries.
    assert al._cached_content_registry == {}


@pytest.mark.asyncio
async def test_ensure_cached_content_recreates_after_expiry():
    """An entry whose local expiry has passed must trigger a fresh
    ``caches.create``."""
    fake_client = MagicMock()
    fake_result = MagicMock()
    fake_result.name = "cachedContents/new"
    fake_client.aio.caches.create = AsyncMock(return_value=fake_result)

    # Pre-populate the registry with an entry that already expired.
    expired_key = al._cache_key("m", _LONG_SYSTEM, [_dummy_tool()])
    al._cached_content_registry[expired_key] = al._CachedEntry(
        name="cachedContents/stale",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    name = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system=_LONG_SYSTEM,
        tool_list=[_dummy_tool()],
    )

    assert name == "cachedContents/new"
    fake_client.aio.caches.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_cached_content_returns_none_when_disabled_via_env():
    """``AGENT_LOOP_CACHE_DISABLED`` wins over everything — the kill
    switch is the operator's escape hatch."""
    fake_client = MagicMock()
    fake_client.aio.caches.create = AsyncMock()

    with patch.object(al, "_CACHE_DISABLED", True):
        name = await al._ensure_cached_content(
            client=fake_client,
            model="m",
            system=_LONG_SYSTEM,
            tool_list=[_dummy_tool()],
        )

    assert name is None
    fake_client.aio.caches.create.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_cached_content_returns_none_when_create_returns_no_name():
    """Defensive: if Gemini ever returns a successful create with an
    empty ``name``, we must not store that and we must fall back."""
    fake_client = MagicMock()
    fake_result = MagicMock()
    fake_result.name = None
    fake_client.aio.caches.create = AsyncMock(return_value=fake_result)

    name = await al._ensure_cached_content(
        client=fake_client,
        model="m",
        system=_LONG_SYSTEM,
        tool_list=[_dummy_tool()],
    )

    assert name is None
    assert al._cached_content_registry == {}
