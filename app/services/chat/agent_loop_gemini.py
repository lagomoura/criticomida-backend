"""Direct Gemini agent loop, bypassing litellm.

This is the alt implementation of ``AgentLoop`` that talks to
``google-genai`` instead of going through litellm. Born out of an
incident where litellm 1.55.4 lost ``thoughtSignature`` on parallel
tool_calls against Vertex Beta us-east4 — a class of bug we cannot
patch without forking. Going direct lets us:

- Read ``thought_signature`` natively from each ``Part`` and round-trip
  it as a real protobuf field, not smuggled inside the tool_call id.
- Get feature parity with whatever Gemini ships day-zero.
- Stay inside a single SDK we already use elsewhere in the codebase
  (``vision_service``, ``chat_title_service``, ``embeddings_service``,
  ``sentiment_service`` all hit Gemini directly).

The public surface is intentionally identical to ``agent_loop.AgentLoop``
so ``chat_service.stream_chat`` can swap implementations behind the
``GEMINI_DIRECT`` flag without touching its own code path.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types as genai_types

from app.services.chat.agent_loop import (
    AgentEvent,
    AssistantTurn,
    ToolRegistry,
)

logger = logging.getLogger(__name__)


# Default model when ``CHAT_MODEL_B2C`` / ``CHAT_MODEL_B2B`` /
# ``CHAT_MODEL`` are unset *and* ``GEMINI_DIRECT`` is on. Bare model
# name (no ``gemini/`` prefix) — google-genai takes plain strings.
_DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite-preview"

# Marker that ``litellm`` 1.55.4 smuggled ``thoughtSignature`` with
# inside the tool_call id. We decode it back when crossing from a
# litellm-persisted history into a direct request, so signatures
# survive the migration.
_THOUGHT_MARKER = "__thought__"


def _strip_provider_prefix(model: str) -> str:
    """Drop the litellm-style provider prefix from a model string.

    google-genai expects plain model names (``gemini-3.1-flash-lite-preview``);
    litellm expects provider-prefixed (``gemini/...``). When the same
    env var feeds both code paths during the convivencia phase, we
    strip the prefix here so callers can keep their litellm-shaped
    config in place.
    """
    for prefix in ("gemini/", "vertex_ai/", "vertex_ai_beta/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def default_b2c_model_gemini() -> str:
    raw = (
        os.getenv("CHAT_MODEL_B2C")
        or os.getenv("CHAT_MODEL")
        or _DEFAULT_GEMINI_MODEL
    )
    return _strip_provider_prefix(raw)


def default_b2b_model_gemini() -> str:
    raw = (
        os.getenv("CHAT_MODEL_B2B")
        or os.getenv("CHAT_MODEL")
        or _DEFAULT_GEMINI_MODEL
    )
    return _strip_provider_prefix(raw)


def default_api_key_gemini() -> str | None:
    """Same source of truth as the litellm path: CHAT_API_KEY first,
    GEMINI_API_KEY as defensive fallback. google-genai accepts the key
    via ``Client(api_key=...)`` and routes to AI Studio when set."""
    return os.getenv("CHAT_API_KEY") or os.getenv("GEMINI_API_KEY") or None


# ──────────────────────────────────────────────────────────────────────
#   OpenAI-shape ↔ Gemini-shape translation
# ──────────────────────────────────────────────────────────────────────


def _split_litellm_id(raw_id: str | None) -> tuple[str, bytes | None]:
    """Decode a litellm-smuggled tool_call id.

    Returns ``(clean_id, signature_bytes_or_None)``. When the id has no
    ``__thought__`` marker — i.e., it was persisted by the new direct
    loop or by an older path that pre-dates signatures — we return the
    id verbatim and ``None``.
    """
    if not raw_id or _THOUGHT_MARKER not in raw_id:
        return raw_id or "", None
    clean, _, b64 = raw_id.partition(_THOUGHT_MARKER)
    try:
        sig = base64.b64decode(b64, validate=False)
    except Exception:
        sig = None
    return clean, sig


def _registry_to_gemini_tools(registry: ToolRegistry) -> list[genai_types.Tool]:
    """Translate a ``ToolRegistry`` to a single ``Tool`` with all
    function declarations. We use ``parametersJsonSchema`` so we can
    pass our own JSONSchema dicts verbatim without rebuilding a
    ``Schema`` model field-by-field."""
    decls: list[genai_types.FunctionDeclaration] = []
    for spec in registry.tools.values():
        decls.append(
            genai_types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters_json_schema=spec.input_schema,
            )
        )
    return [genai_types.Tool(function_declarations=decls)]


def _messages_to_contents(
    messages: list[dict[str, Any]],
) -> list[genai_types.Content]:
    """Translate the OpenAI-shape messages list into Gemini ``Content``s.

    Input shape (matches what ``chat_service._load_history`` returns
    plus the freshly-appended user turn):

    - ``{"role": "user", "content": "..."}``
    - ``{"role": "assistant", "content": "...", "tool_calls": [{...}]?}``
    - ``{"role": "tool", "tool_call_id": "...", "content": "..."}``

    Gemini convention:
    - user text → ``Content(role="user", parts=[Part.from_text(...)])``
    - assistant text + tool calls → ``Content(role="model", parts=[
        Part.from_text(...), Part(function_call=..., thought_signature=...)
      ])``
    - tool result → ``Content(role="user", parts=[Part.from_function_response(
        name=..., response={"result": ...})])``

    We collapse consecutive ``tool`` rows into a single ``user``-role
    ``Content`` with one ``function_response`` part each — that's how
    Gemini expects parallel tool results to come back.
    """
    contents: list[genai_types.Content] = []
    pending_tool_parts: list[genai_types.Part] = []

    # Index assistant tool_calls by id so we can resolve names when we
    # see the corresponding tool message (which only carries the id).
    tool_name_by_id: dict[str, str] = {}

    def flush_tool_parts() -> None:
        if pending_tool_parts:
            contents.append(
                genai_types.Content(role="user", parts=list(pending_tool_parts))
            )
            pending_tool_parts.clear()

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "tool":
            tcid = msg.get("tool_call_id") or ""
            clean_id, _ = _split_litellm_id(tcid)
            name = tool_name_by_id.get(clean_id) or tool_name_by_id.get(tcid) or "unknown"
            try:
                parsed = json.loads(content) if content else {}
            except (TypeError, ValueError):
                parsed = {"raw": content}
            pending_tool_parts.append(
                genai_types.Part.from_function_response(
                    name=name,
                    response={"result": parsed},
                )
            )
            continue

        # Anything that isn't a tool flushes any pending function_responses.
        flush_tool_parts()

        if role == "user":
            contents.append(
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=content)],
                )
            )
        elif role == "assistant":
            parts: list[genai_types.Part] = []
            if content:
                parts.append(genai_types.Part.from_text(text=content))
            for tc in msg.get("tool_calls") or []:
                raw_id = tc.get("id") or ""
                clean_id, smuggled_sig = _split_litellm_id(raw_id)
                # Prefer an explicit field if the new loop persisted it.
                explicit_sig = tc.get("thought_signature")
                if isinstance(explicit_sig, str):
                    try:
                        explicit_sig = base64.b64decode(explicit_sig, validate=False)
                    except Exception:
                        explicit_sig = None
                signature = explicit_sig if explicit_sig else smuggled_sig
                args_raw = tc.get("function", {}).get("arguments") or tc.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                except (TypeError, ValueError):
                    args = {}
                name = tc.get("function", {}).get("name") or tc.get("name") or "unknown"
                tool_name_by_id[clean_id] = name
                tool_name_by_id[raw_id] = name
                fc_part = genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id=clean_id or None,
                        name=name,
                        args=args,
                    ),
                    thought_signature=signature,
                )
                parts.append(fc_part)
            if parts:
                contents.append(genai_types.Content(role="model", parts=parts))
        # other roles ignored

    flush_tool_parts()
    return contents


# ──────────────────────────────────────────────────────────────────────
#   Agent loop (Gemini direct)
# ──────────────────────────────────────────────────────────────────────


class AgentLoop:
    """Drop-in replacement for ``agent_loop.AgentLoop`` that uses
    ``google-genai`` directly. Same constructor signature, same ``run``
    iterator contract, same ``AgentEvent`` types — chat_service should
    not need to know which one it's holding."""

    def __init__(
        self,
        *,
        model: str,
        registry: ToolRegistry,
        max_iterations: int = 5,
        max_tokens: int = 1024,
        api_key: str | None = None,
    ) -> None:
        self.model = _strip_provider_prefix(model)
        self.registry = registry
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.api_key = api_key
        # google-genai picks AI Studio when ``api_key`` is given; if
        # we ever need Vertex (e.g. preview-only model not in AI
        # Studio's catalog), this is where we'd flip
        # ``vertexai=True`` + project/location.
        self._client = (
            genai.Client(api_key=api_key) if api_key else genai.Client()
        )

    async def run(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop and yield events as they happen.

        ``messages`` follows the same OpenAI-shaped contract the litellm
        path uses (so ``chat_service._load_history`` keeps working);
        the implementation translates to Gemini's ``Content`` / ``Part``
        shape internally.
        """
        contents = _messages_to_contents(messages)
        tool_list = _registry_to_gemini_tools(self.registry)

        for iteration in range(self.max_iterations):
            config = genai_types.GenerateContentConfig(
                system_instruction=system,
                tools=tool_list,
                max_output_tokens=self.max_tokens,
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True,  # we run the loop manually
                ),
            )

            stream_started = time.monotonic()

            # Accumulators for the streamed assistant turn.
            assistant_text_chunks: list[str] = []
            assistant_parts: list[genai_types.Part] = []
            tool_calls_for_persist: list[dict[str, Any]] = []
            input_tokens: int | None = None
            output_tokens: int | None = None

            try:
                stream = await self._client.aio.models.generate_content_stream(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                async for chunk in stream:
                    usage = getattr(chunk, "usage_metadata", None)
                    if usage is not None:
                        if usage.prompt_token_count is not None:
                            input_tokens = usage.prompt_token_count
                        if usage.candidates_token_count is not None:
                            output_tokens = usage.candidates_token_count

                    cands = getattr(chunk, "candidates", None) or []
                    if not cands:
                        continue
                    cand = cands[0]
                    cont = getattr(cand, "content", None)
                    if cont is None or not cont.parts:
                        continue

                    for part in cont.parts:
                        # Preserve the part as Gemini sent it for the
                        # next-turn assistant Content. Echoing the same
                        # objects (with thought_signature in place) is
                        # what makes Vertex stop yelling about position N.
                        assistant_parts.append(part)

                        if part.text and not part.thought:
                            assistant_text_chunks.append(part.text)
                            yield AgentEvent("text_delta", part.text)

                        if part.function_call is not None:
                            fc = part.function_call
                            call_id = fc.id or f"call_{uuid.uuid4().hex[:12]}"
                            sig_bytes = part.thought_signature
                            sig_b64 = (
                                base64.b64encode(sig_bytes).decode("ascii")
                                if sig_bytes
                                else None
                            )
                            tool_calls_for_persist.append(
                                {
                                    "id": call_id,
                                    "name": fc.name,
                                    "arguments": json.dumps(
                                        dict(fc.args) if fc.args else {},
                                        default=str,
                                    ),
                                    # Stored as base64 so the DB JSONB
                                    # column stays plain text; we
                                    # decode on the way back in
                                    # _messages_to_contents.
                                    "thought_signature": sig_b64,
                                }
                            )
            except Exception as exc:  # network, auth, validation, etc.
                logger.exception("gemini.generate_content_stream failed")
                yield AgentEvent("error", f"LLM call failed: {exc}")
                return

            assistant_text = "".join(assistant_text_chunks)
            tool_calls_list = tool_calls_for_persist or None

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
                "agent_loop_gemini iter=%d tools=%d duration_ms=%d",
                iteration,
                len(tool_calls_list or []),
                int((time.monotonic() - stream_started) * 1000),
            )

            if not tool_calls_list:
                yield AgentEvent("done", None)
                return

            # Append the assistant Content verbatim — same Part objects
            # we just streamed, including thought_signatures.
            contents.append(
                genai_types.Content(role="model", parts=list(assistant_parts))
            )

            # Execute tools sequentially; each one becomes a
            # function_response Part appended in a single user-role
            # Content (Gemini's convention for multi-tool returns).
            tool_response_parts: list[genai_types.Part] = []
            for tc in tool_calls_list:
                tool_id = tc["id"]
                tool_name = tc["name"]
                spec = self.registry.tools.get(tool_name)
                try:
                    parsed_args = json.loads(tc["arguments"] or "{}")
                except (TypeError, ValueError) as exc:
                    err = {"error": f"Invalid JSON arguments: {exc}"}
                    yield AgentEvent(
                        "tool_call_result",
                        {"id": tool_id, "name": tool_name, "output": err, "is_error": True},
                    )
                    tool_response_parts.append(
                        genai_types.Part.from_function_response(
                            name=tool_name,
                            response={"result": err},
                        )
                    )
                    continue

                if spec is None:
                    err = {"error": f"Unknown tool: {tool_name}"}
                    yield AgentEvent(
                        "tool_call_result",
                        {"id": tool_id, "name": tool_name, "output": err, "is_error": True},
                    )
                    tool_response_parts.append(
                        genai_types.Part.from_function_response(
                            name=tool_name,
                            response={"result": err},
                        )
                    )
                    continue

                yield AgentEvent(
                    "tool_call_start",
                    {"id": tool_id, "name": tool_name, "input": parsed_args},
                )

                try:
                    import asyncio
                    output = await asyncio.wait_for(
                        spec.handler(parsed_args),
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

                tool_response_parts.append(
                    genai_types.Part.from_function_response(
                        name=tool_name,
                        response={"result": output},
                    )
                )

            contents.append(
                genai_types.Content(role="user", parts=tool_response_parts)
            )

        # Iteration cap exhausted.
        yield AgentEvent(
            "error",
            f"Agent loop exceeded max_iterations={self.max_iterations}",
        )


__all__ = [
    "AgentLoop",
    "AssistantTurn",
    "default_b2c_model_gemini",
    "default_b2b_model_gemini",
    "default_api_key_gemini",
]
