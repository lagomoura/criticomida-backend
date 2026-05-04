"""Tests for the strict-turn-grammar sanitiser.

Vertex Gemini rejects message arrays where a ``function_call`` (an
assistant turn with ``tool_calls``) does not follow a ``user`` or a
``function_response`` (a ``role='tool'`` turn). The sanitiser fixes
two common ways the slice can drift into that shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.chat_service import _sanitize_for_strict_turn_grammar


@dataclass
class FakeMessage:
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


def _build(*items: tuple[str, ...]) -> list[FakeMessage]:
    out: list[FakeMessage] = []
    for item in items:
        if item[0] == "assistant_tools":
            out.append(
                FakeMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[{"id": "call_1", "name": "x", "arguments": "{}"}],
                )
            )
        elif item[0] == "assistant_text":
            out.append(FakeMessage(role="assistant", content=item[1] if len(item) > 1 else "ok"))
        elif item[0] == "user":
            out.append(FakeMessage(role="user", content=item[1] if len(item) > 1 else "hi"))
        elif item[0] == "tool":
            out.append(FakeMessage(role="tool"))
    return out


class TestLeadingNonUser:
    def test_orphan_tool_at_head_is_dropped(self):
        rows = _build(("tool",), ("assistant_text",), ("user",), ("assistant_text",))
        cleaned = _sanitize_for_strict_turn_grammar(rows)
        # Drop tool, then assistant_text (also non-user), keep from 'user'.
        assert [m.role for m in cleaned] == ["user", "assistant"]

    def test_orphan_assistant_with_tool_calls_at_head_is_dropped(self):
        rows = _build(
            ("assistant_tools",),
            ("tool",),
            ("user",),
            ("assistant_text",),
        )
        cleaned = _sanitize_for_strict_turn_grammar(rows)
        assert [m.role for m in cleaned] == ["user", "assistant"]

    def test_already_starting_with_user_is_preserved(self):
        rows = _build(
            ("user",),
            ("assistant_tools",),
            ("tool",),
            ("assistant_text",),
        )
        cleaned = _sanitize_for_strict_turn_grammar(rows)
        assert [m.role for m in cleaned] == ["user", "assistant", "tool", "assistant"]


class TestTrailingOrphanToolCalls:
    def test_trailing_assistant_with_tool_calls_is_dropped(self):
        rows = _build(
            ("user",),
            ("assistant_text",),
            ("user",),
            ("assistant_tools",),  # orphan: no tool responses follow
        )
        cleaned = _sanitize_for_strict_turn_grammar(rows)
        assert [m.role for m in cleaned] == ["user", "assistant", "user"]

    def test_trailing_assistant_text_is_kept(self):
        rows = _build(
            ("user",),
            ("assistant_text",),
        )
        cleaned = _sanitize_for_strict_turn_grammar(rows)
        assert [m.role for m in cleaned] == ["user", "assistant"]

    def test_trailing_tool_response_is_kept(self):
        rows = _build(
            ("user",),
            ("assistant_tools",),
            ("tool",),
        )
        cleaned = _sanitize_for_strict_turn_grammar(rows)
        # Valid: model(func_call) → function. Append user → user. Fine.
        assert [m.role for m in cleaned] == ["user", "assistant", "tool"]


class TestEmpty:
    def test_empty_input_yields_empty(self):
        assert _sanitize_for_strict_turn_grammar([]) == []

    def test_only_non_user_yields_empty(self):
        rows = _build(("tool",), ("assistant_text",), ("tool",))
        assert _sanitize_for_strict_turn_grammar(rows) == []
