"""Eval runner for the CritiComida chat agents.

Each eval case is a YAML record with:

- The ``user_input`` we send through the chat agent (real LLM, real
  tools, real DB fixture).
- A set of ``expected`` assertions we evaluate after the loop finishes.

The runner is deliberately small: it iterates the ``AgentLoop`` event
stream, captures the tool calls fired and the assistant's final text,
then runs each assertion as a boolean predicate. We don't try to mimic
every possible kind of check — the assertion vocabulary is intentionally
narrow so cases stay readable.

This is the "Phase 3" / audit layer of the multi-language tool contract:
unit tests prove the contract is enforced inside the handler, but only an
end-to-end run (with the real model picking the enum from natural
language) tells us whether the polyglot mapping actually works.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatAgent
from app.services.chat.agent_loop import AgentLoop, default_b2b_model
from app.services.chat.prompts.loader import load_agent_prompt
from app.services.chat.tools.registry import build_registry


# ──────────────────────────────────────────────────────────────────────────
#   Case schema
# ──────────────────────────────────────────────────────────────────────────


class ExpectedToolCall(BaseModel):
    """One tool call we expect the agent to make.

    ``args_must_match`` is a partial dict — only the keys listed have
    to match exactly. Other arguments the model fills in (limit, sort,
    etc.) are ignored unless explicitly asserted.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    args_must_match: dict[str, Any] = Field(default_factory=dict)


class CaseExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools_called: list[ExpectedToolCall] = Field(default_factory=list)
    response_must_contain_any: list[str] = Field(default_factory=list)
    response_must_not_contain: list[str] = Field(default_factory=list)
    no_tool_errors: bool = True


class EvalCase(BaseModel):
    """One YAML case in the dataset."""

    model_config = ConfigDict(extra="forbid")

    id: str
    locale: Literal["es", "en", "pt", "mixed"] = "es"
    description: str | None = None
    user_input: str
    expected: CaseExpectations


# ──────────────────────────────────────────────────────────────────────────
#   Run + result
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class CapturedToolCall:
    name: str
    args: dict[str, Any]
    output: dict[str, Any]
    is_error: bool


@dataclass
class EvalResult:
    case_id: str
    passed: bool
    final_text: str
    tool_calls: list[CapturedToolCall] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    iterations: int = 0


async def run_eval_case(
    case: EvalCase,
    *,
    db: AsyncSession,
    agent: ChatAgent,
    restaurant_scope_id: str,
    user_id: Any | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_iterations: int = 5,
) -> EvalResult:
    """Run one eval case against the real agent loop.

    Caller is responsible for setting up the DB fixture (restaurant +
    reviews) and tearing it down. We only own the agent invocation.
    Pass ``user_id`` for tools that bind to an authenticated owner
    (e.g. ``update_owner_preferences``); leave ``None`` for the
    anonymous flow that most cases need.
    """
    system_prompt = load_agent_prompt(agent)
    registry = build_registry(
        agent=agent,
        db=db,
        user_id=user_id,
        embed_query=None,
        restaurant_scope_id=restaurant_scope_id,
    )
    loop = AgentLoop(
        model=model or default_b2b_model(),
        registry=registry,
        max_iterations=max_iterations,
        max_tokens=1024,
        api_key=api_key,
    )

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": case.user_input}
    ]

    pending_calls: dict[str, dict[str, Any]] = {}
    captured: list[CapturedToolCall] = []
    final_text_parts: list[str] = []
    iterations = 0

    async for event in loop.run(system=system_prompt, messages=messages):
        if event.type == "tool_call_start":
            data = event.data
            pending_calls[data["id"]] = {"name": data["name"], "input": data["input"]}
        elif event.type == "tool_call_result":
            data = event.data
            pending = pending_calls.pop(data["id"], {})
            captured.append(
                CapturedToolCall(
                    name=data["name"],
                    args=pending.get("input", {}),
                    output=data["output"],
                    is_error=bool(data.get("is_error", False)),
                )
            )
        elif event.type == "message_complete":
            iterations += 1
            text = getattr(event.data, "content", "") or ""
            # The final text is whatever the model said in the last
            # iteration with no tool_calls. Track both: we'll keep only
            # the last non-empty content as "final".
            if text:
                final_text_parts.append(text)
        elif event.type == "error":
            return EvalResult(
                case_id=case.id,
                passed=False,
                final_text="",
                tool_calls=captured,
                failures=[f"agent_loop emitted error: {event.data!r}"],
                iterations=iterations,
            )
        elif event.type == "done":
            break

    # Final text is the last assistant turn that produced text. If the
    # model only emitted tool calls (no narration in any iteration), we
    # leave it empty — that itself is usually a fail signal.
    final_text = final_text_parts[-1] if final_text_parts else ""

    failures = _check_assertions(case, captured, final_text)
    return EvalResult(
        case_id=case.id,
        passed=not failures,
        final_text=final_text,
        tool_calls=captured,
        failures=failures,
        iterations=iterations,
    )


# ──────────────────────────────────────────────────────────────────────────
#   Assertion helpers
# ──────────────────────────────────────────────────────────────────────────


def _check_assertions(
    case: EvalCase,
    tool_calls: list[CapturedToolCall],
    final_text: str,
) -> list[str]:
    failures: list[str] = []

    if case.expected.no_tool_errors:
        bad = [tc for tc in tool_calls if tc.is_error]
        if bad:
            failures.append(
                "tool errors observed: "
                + json.dumps(
                    [{"name": tc.name, "output": tc.output} for tc in bad],
                    ensure_ascii=False,
                )[:400]
            )

    for expected in case.expected.tools_called:
        match = _find_matching_call(tool_calls, expected)
        if match is None:
            failures.append(
                f"expected tool '{expected.name}' with args ⊇ "
                f"{expected.args_must_match!r} not called. Got calls: "
                + json.dumps(
                    [{"name": tc.name, "args": tc.args} for tc in tool_calls],
                    ensure_ascii=False,
                )[:300]
            )

    if case.expected.response_must_contain_any:
        text_norm = final_text.lower()
        if not any(
            needle.lower() in text_norm
            for needle in case.expected.response_must_contain_any
        ):
            failures.append(
                "final response did not contain any of "
                f"{case.expected.response_must_contain_any!r}. Got: {final_text[:200]!r}"
            )

    text_lower = final_text.lower()
    for forbidden in case.expected.response_must_not_contain:
        # Plain case-insensitive substring match — using ``re.search``
        # would interpret ``$``, ``.``, ``(`` etc. as regex anchors and
        # bite us in cases like asserting "no $ symbol in the reply".
        # Authors of YAML cases are not expected to think in regex.
        if forbidden.lower() in text_lower:
            failures.append(
                f"final response contained forbidden pattern {forbidden!r}: "
                f"{final_text[:200]!r}"
            )

    return failures


def _find_matching_call(
    captured: list[CapturedToolCall], expected: ExpectedToolCall
) -> CapturedToolCall | None:
    """Subset match with case-insensitive string equality.

    The tool contract normalises casing (Pydantic ``before`` validator
    lowercases enum strings), so an LLM that emits ``'NEUTRAL'`` and one
    that emits ``'neutral'`` are behaviourally identical. Asserting
    against the raw payload would flake on case alone.
    """
    for tc in captured:
        if tc.name != expected.name:
            continue
        match = True
        for key, expected_value in expected.args_must_match.items():
            actual = tc.args.get(key)
            if isinstance(actual, str) and isinstance(expected_value, str):
                if actual.lower() != expected_value.lower():
                    match = False
                    break
            elif actual != expected_value:
                match = False
                break
        if match:
            return tc
    return None
