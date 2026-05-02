"""Agentic tool-use loop for the CritiComida chatbot.

This is the core orchestrator. It deliberately avoids LangChain/LangGraph
because the surface needed (single-shot tool loop with cap, streaming
deltas, structured tool registry) is small enough that the abstraction
overhead would hurt debuggability more than it helps.

Flow:

    1. Caller passes ``messages`` (system + history + new user turn) and a
       ``ToolRegistry``.
    2. ``run`` calls litellm with the registry's JSONSchema tool specs and
       streams tokens as ``AgentEvent``s.
    3. When the model emits ``tool_use`` blocks, we execute them one by
       one (sequentially: tools may depend on prior state), append the
       results as ``role='tool'`` messages, and recurse.
    4. The loop stops when:
       - the model returns ``stop_reason='end_turn'`` (no more tools);
       - we hit ``max_iterations`` (5 by default — guardrail);
       - or a tool raises a fatal error (we still surface it as an event
         and finish gracefully).

Each tool execution gets a per-call timeout. Failures are captured as
``tool_result`` content with ``is_error=True`` so the model can recover
instead of the whole turn dying.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import litellm

logger = logging.getLogger(__name__)


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a pydantic model OR plain dict OR None."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ──────────────────────────────────────────────────────────────────────────
#   Tool registry
# ──────────────────────────────────────────────────────────────────────────


ToolFunc = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    """A single tool the agent can call.

    ``handler`` receives validated kwargs (already parsed JSON) and must
    return a JSON-serializable dict. The dict is sent back to the model as
    a ``tool_result`` and is also persisted in ``chat_messages.tool_result``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]  # JSONSchema for the tool input
    handler: ToolFunc
    timeout_seconds: float = 8.0
    # Set True for tools whose output should also bubble up as a UI card
    # event (e.g. search_dishes returns dish cards the frontend renders).
    emits_card: bool = False


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"Tool '{spec.name}' already registered")
        self.tools[spec.name] = spec

    def to_anthropic_tools(self) -> list[dict[str, Any]]:
        """Build the tool list passed to litellm in Anthropic format.

        litellm accepts the OpenAI ``function`` format too, but Anthropic
        is our default provider and the native shape avoids translation.
        """
        return [
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.input_schema,
            }
            for s in self.tools.values()
        ]


# ──────────────────────────────────────────────────────────────────────────
#   Event stream
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class AgentEvent:
    """An event emitted by the agent loop.

    Types:

    - ``text_delta`` — partial assistant text token (data: str).
    - ``tool_call_start`` — tool about to run (data: {id, name, input}).
    - ``tool_call_result`` — tool finished (data: {id, name, output, is_error}).
    - ``card`` — tool flagged ``emits_card``; data is the tool output for
      the frontend renderers.
    - ``message_complete`` — one assistant message finished; data carries
      the full assistant content + tool_calls + token counts to persist.
    - ``done`` — whole turn finished, no more events.
    - ``error`` — fatal error; data is a string describing it.
    """

    type: str
    data: Any


# ──────────────────────────────────────────────────────────────────────────
#   Agent loop
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class AssistantTurn:
    """Persisted snapshot of one assistant message produced inside the loop."""

    content: str
    tool_calls: list[dict[str, Any]] | None
    input_tokens: int | None
    output_tokens: int | None


class AgentLoop:
    def __init__(
        self,
        *,
        model: str,
        registry: ToolRegistry,
        max_iterations: int = 5,
        max_tokens: int = 1024,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.api_key = api_key

    async def run(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop and yield events as they happen.

        ``messages`` is mutated in-place across iterations so callers can
        pass a copy if they need the original list intact.
        """
        for iteration in range(self.max_iterations):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "system": system,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "tools": self.registry.to_anthropic_tools(),
                "stream": True,
            }
            if self.api_key:
                kwargs["api_key"] = self.api_key

            stream_started = time.monotonic()
            try:
                response_stream = await litellm.acompletion(**kwargs)
            except Exception as exc:  # network, auth, validation
                logger.exception("litellm.acompletion failed")
                yield AgentEvent("error", f"LLM call failed: {exc}")
                return

            assistant_text_chunks: list[str] = []
            tool_calls_in_progress: dict[int, dict[str, Any]] = {}
            stop_reason: str | None = None
            input_tokens: int | None = None
            output_tokens: int | None = None

            async for chunk in response_stream:
                # litellm normalizes Anthropic's event shapes to OpenAI's
                # streaming delta. Use _attr to read both pydantic models
                # and plain dicts.
                usage = _attr(chunk, "usage")
                if usage is not None:
                    pt = _attr(usage, "prompt_tokens")
                    ct = _attr(usage, "completion_tokens")
                    if pt is not None:
                        input_tokens = pt
                    if ct is not None:
                        output_tokens = ct

                choices = _attr(chunk, "choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = _attr(choice, "delta") or {}

                content_delta = _attr(delta, "content")
                if content_delta:
                    assistant_text_chunks.append(content_delta)
                    yield AgentEvent("text_delta", content_delta)

                for tc in _attr(delta, "tool_calls") or []:
                    idx = _attr(tc, "index") or 0
                    bucket = tool_calls_in_progress.setdefault(
                        idx,
                        {"id": None, "name": None, "arguments": ""},
                    )
                    tc_id = _attr(tc, "id")
                    if tc_id:
                        bucket["id"] = tc_id
                    fn = _attr(tc, "function")
                    if fn is not None:
                        fn_name = _attr(fn, "name")
                        if fn_name:
                            bucket["name"] = fn_name
                        fn_args = _attr(fn, "arguments")
                        if fn_args:
                            bucket["arguments"] += fn_args

                finish = _attr(choice, "finish_reason")
                if finish:
                    stop_reason = finish

            assistant_text = "".join(assistant_text_chunks)

            ordered_calls = [
                bucket
                for _, bucket in sorted(tool_calls_in_progress.items())
                if bucket["name"]
            ]
            for bucket in ordered_calls:
                if not bucket["id"]:
                    bucket["id"] = f"call_{uuid.uuid4().hex[:12]}"
            tool_calls_list = ordered_calls or None

            # Snapshot the assistant turn so the caller can persist it.
            yield AgentEvent(
                "message_complete",
                AssistantTurn(
                    content=assistant_text,
                    tool_calls=tool_calls_list,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )

            logger.info(
                "agent_loop iteration=%d stop=%s tools=%d duration_ms=%d",
                iteration,
                stop_reason,
                len(tool_calls_list or []),
                int((time.monotonic() - stream_started) * 1000),
            )

            if not tool_calls_list:
                yield AgentEvent("done", None)
                return

            # Append the assistant message verbatim (with tool calls) so
            # the next iteration can carry the tool_use_ids.
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_text or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"] or "{}",
                            },
                        }
                        for tc in tool_calls_list
                    ],
                }
            )

            # Execute each tool sequentially; results feed back as a single
            # role='tool' message per call, matching the OpenAI shape that
            # litellm expects on the next request.
            for tc in tool_calls_list:
                tool_id = tc["id"]
                tool_name = tc["name"]
                spec = self.registry.tools.get(tool_name)
                if spec is None:
                    err_payload = {
                        "error": f"Unknown tool: {tool_name}",
                    }
                    yield AgentEvent(
                        "tool_call_result",
                        {
                            "id": tool_id,
                            "name": tool_name,
                            "output": err_payload,
                            "is_error": True,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": json.dumps(err_payload),
                        }
                    )
                    continue

                try:
                    parsed = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError as exc:
                    err_payload = {
                        "error": f"Invalid JSON arguments: {exc}",
                    }
                    yield AgentEvent(
                        "tool_call_result",
                        {
                            "id": tool_id,
                            "name": tool_name,
                            "output": err_payload,
                            "is_error": True,
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": json.dumps(err_payload),
                        }
                    )
                    continue

                yield AgentEvent(
                    "tool_call_start",
                    {"id": tool_id, "name": tool_name, "input": parsed},
                )

                try:
                    output = await asyncio.wait_for(
                        spec.handler(parsed),
                        timeout=spec.timeout_seconds,
                    )
                    is_error = False
                except asyncio.TimeoutError:
                    output = {
                        "error": (
                            f"Tool '{tool_name}' timed out after "
                            f"{spec.timeout_seconds}s"
                        )
                    }
                    is_error = True
                except Exception as exc:
                    logger.exception("tool '%s' raised", tool_name)
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                    is_error = True

                yield AgentEvent(
                    "tool_call_result",
                    {
                        "id": tool_id,
                        "name": tool_name,
                        "output": output,
                        "is_error": is_error,
                    },
                )
                if spec.emits_card and not is_error:
                    yield AgentEvent("card", {"name": tool_name, "data": output})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": json.dumps(output, default=str),
                    }
                )

            # Loop back: another LLM call now sees the tool results.

        # Iteration cap exhausted.
        yield AgentEvent(
            "error",
            f"Agent loop exceeded max_iterations={self.max_iterations}",
        )


def default_b2c_model() -> str:
    return os.getenv("CHAT_MODEL_B2C") or os.getenv(
        "CHAT_MODEL", "anthropic/claude-haiku-4-5-20251001"
    )


def default_b2b_model() -> str:
    return os.getenv("CHAT_MODEL_B2B", "anthropic/claude-sonnet-4-6")


def default_api_key() -> str | None:
    return os.getenv("CHAT_API_KEY") or None
