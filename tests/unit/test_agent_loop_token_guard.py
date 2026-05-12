"""Unit tests for the context-window guard in ``agent_loop``.

Covers two pieces of correctness:

1. ``_into_blocks`` keeps a ``function_call`` glued to its matching
   ``function_response`` — Gemini rejects orphan halves of a tool
   round-trip, and the truncator must drop pairs atomically.
2. ``_truncate_contents_to_fit`` drops oldest blocks until the
   ``count_tokens`` mock falls below the cap, and always preserves
   the most recent block (the live user turn).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from google.genai import types as genai_types

from app.services.chat import agent_loop as al


def _user_text(text: str) -> genai_types.Content:
    return genai_types.Content(
        role="user", parts=[genai_types.Part.from_text(text=text)]
    )


def _model_text(text: str) -> genai_types.Content:
    return genai_types.Content(
        role="model", parts=[genai_types.Part.from_text(text=text)]
    )


def _model_function_call(name: str = "search_dishes") -> genai_types.Content:
    return genai_types.Content(
        role="model",
        parts=[
            genai_types.Part(
                function_call=genai_types.FunctionCall(name=name, args={})
            )
        ],
    )


def _user_function_response(name: str = "search_dishes") -> genai_types.Content:
    return genai_types.Content(
        role="user",
        parts=[
            genai_types.Part.from_function_response(
                name=name, response={"ok": True}
            )
        ],
    )


# --- _into_blocks / _from_blocks ------------------------------------------


def test_into_blocks_pairs_function_call_with_response():
    contents = [
        _user_text("hola"),
        _model_function_call(),
        _user_function_response(),
        _model_text("aquí van"),
    ]
    blocks = al._into_blocks(contents)
    assert [len(b) for b in blocks] == [1, 2, 1]
    # Round-trip: re-flattening recovers the original sequence.
    assert al._from_blocks(blocks) == contents


def test_into_blocks_isolated_function_call_stays_alone():
    """A model turn with function_call **without** a following user
    function_response (truncated history, streaming mid-flight) is
    kept as a single-row block — not glued to whatever comes next."""
    contents = [
        _model_function_call(),
        _model_text("texto raro"),
    ]
    blocks = al._into_blocks(contents)
    assert [len(b) for b in blocks] == [1, 1]


def test_into_blocks_handles_empty_input():
    assert al._into_blocks([]) == []


# --- _truncate_contents_to_fit --------------------------------------------


@pytest.mark.asyncio
async def test_truncate_skipped_when_history_is_short():
    """Short histories must not trigger a count_tokens round-trip — the
    guard is supposed to be a no-op on turn 1."""
    contents = [_user_text("hola")] * 3
    fake_client = MagicMock()
    fake_client.aio.models.count_tokens = AsyncMock()

    out, total = await al._truncate_contents_to_fit(
        client=fake_client,
        model="m",
        system="s",
        tool_list=[],
        contents=contents,
    )

    assert out == contents
    assert total is None
    fake_client.aio.models.count_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_truncate_no_op_when_under_cap():
    """Above the row threshold but well under the token cap → keep
    contents unchanged after the single count_tokens call."""
    contents = [_user_text(f"msg-{i}") for i in range(20)]
    fake_response = MagicMock()
    fake_response.total_tokens = 1000
    fake_client = MagicMock()
    fake_client.aio.models.count_tokens = AsyncMock(return_value=fake_response)

    out, total = await al._truncate_contents_to_fit(
        client=fake_client,
        model="m",
        system="s",
        tool_list=[],
        contents=contents,
        cap=100_000,
    )

    assert out == contents
    assert total == 1000
    # Exactly one count_tokens call — no truncation attempts.
    assert fake_client.aio.models.count_tokens.await_count == 1


@pytest.mark.asyncio
async def test_truncate_drops_oldest_blocks_until_under_cap():
    """Simulate a chat over the cap: count_tokens reports decreasing
    counts after each drop until we fall below the cap. The function
    must drop one block per attempt and stop as soon as we're under."""
    contents = [_user_text(f"msg-{i}") for i in range(20)]
    # Sequence: initial=2000, after-1-drop=1500, after-2-drops=900.
    counts = [2000, 1500, 900]
    responses = [MagicMock(total_tokens=n) for n in counts]
    fake_client = MagicMock()
    fake_client.aio.models.count_tokens = AsyncMock(side_effect=responses)

    out, total = await al._truncate_contents_to_fit(
        client=fake_client,
        model="m",
        system="s",
        tool_list=[],
        contents=contents,
        cap=1000,
    )

    # 20 blocks initially, 2 dropped → 18 remaining.
    assert len(out) == 18
    # Preserves the newest content as the tail of the list.
    assert out[-1] == contents[-1]
    assert total == 900
    assert fake_client.aio.models.count_tokens.await_count == 3


@pytest.mark.asyncio
async def test_truncate_keeps_function_call_pairs_intact():
    """When the truncator drops a pair, it must drop both halves
    together — never an orphan ``function_call`` or
    ``function_response``."""
    contents = [
        _model_function_call("a"),  # pair 1, block index 0
        _user_function_response("a"),
        _user_text("turn-1-question"),  # block index 1
        _model_function_call("b"),  # pair 2, block index 2
        _user_function_response("b"),
        _user_text("turn-2-question"),  # block index 3 (newest, preserved)
    ]
    # Over cap initially, under cap after the first drop.
    fake_client = MagicMock()
    fake_client.aio.models.count_tokens = AsyncMock(
        side_effect=[
            MagicMock(total_tokens=5000),  # initial
            MagicMock(total_tokens=800),   # after dropping block 0 (the pair)
        ]
    )

    # ``min_rows=0`` forces the guard to engage on a short fixture; the
    # invariant we want to verify (atomic pair drops) is independent of
    # the threshold, but we need the function to actually run.
    out, total = await al._truncate_contents_to_fit(
        client=fake_client,
        model="m",
        system="s",
        tool_list=[],
        contents=contents,
        cap=1000,
        min_rows=0,
    )

    # The dropped block was the leading pair — both its rows are gone,
    # never just one.
    assert not any(
        al._is_function_call_content(c) and c.parts[0].function_call.name == "a"
        for c in out
    )
    assert not any(
        al._is_function_response_content(c)
        and any(p.function_response.name == "a" for p in c.parts)
        for c in out
    )
    # Pair "b" survives intact.
    assert al._is_function_call_content(out[1])
    assert al._is_function_response_content(out[2])
    assert total == 800


@pytest.mark.asyncio
async def test_truncate_swallows_count_tokens_failure():
    """count_tokens is best-effort — if it fails (network, auth, quota)
    we must proceed with the unmodified contents so the user's turn
    still has a chance to land. The live call will surface any real
    overflow downstream."""
    contents = [_user_text(f"msg-{i}") for i in range(20)]
    fake_client = MagicMock()
    fake_client.aio.models.count_tokens = AsyncMock(
        side_effect=RuntimeError("network")
    )

    out, total = await al._truncate_contents_to_fit(
        client=fake_client,
        model="m",
        system="s",
        tool_list=[],
        contents=contents,
    )

    assert out == contents
    assert total is None
