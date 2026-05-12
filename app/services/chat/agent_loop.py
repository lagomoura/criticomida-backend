"""Agentic tool-use loop for the Palato chatbot.

Talks to Gemini directly via ``google-genai`` (no litellm). Started
life as a litellm-based loop; we removed litellm after a parallel
``thoughtSignature`` bug in 1.55.4 against Vertex Beta us-east4 that
we couldn't fix without forking. Going direct also lets us round-trip
``thought_signature`` as a real protobuf field on each ``Part``
instead of smuggling it inside tool_call ids.

Flow:

    1. Caller passes ``messages`` (system + history + new user turn) and
       a ``ToolRegistry``.
    2. ``run`` translates ``messages`` to Gemini ``Content`` / ``Part``
       shape, opens a streaming ``generate_content_stream``, and yields
       ``AgentEvent``s as text/function_call parts arrive.
    3. When the model emits ``function_call`` parts, we execute them
       sequentially, append ``function_response`` parts to the
       conversation, and recurse.
    4. The loop stops when:
       - the model returns a turn with no tool calls (final answer);
       - we hit ``max_iterations`` (5 by default — guardrail);
       - or a tool raises a fatal error (we still surface it as an
         event and finish gracefully).

Each tool execution gets a per-call timeout. Failures are captured as
``function_response`` content with ``error`` so the model can recover
instead of the whole turn dying.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from google import genai
from google.genai import types as genai_types

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
#   Tool registry
# ──────────────────────────────────────────────────────────────────────────


ToolFunc = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    """A single tool the agent can call.

    ``handler`` receives validated kwargs (already parsed JSON) and must
    return a JSON-serializable dict. The dict is sent back to the model
    as a ``function_response`` Part and is also persisted in
    ``chat_messages.tool_result``.
    """

    name: str
    description: str
    input_schema: dict[str, Any]  # JSONSchema for the tool input
    handler: ToolFunc
    timeout_seconds: float = 8.0
    # Set True for tools whose output should also bubble up as a UI card
    # event (e.g. recommend_dishes returns dish cards the frontend renders).
    emits_card: bool = False


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"Tool '{spec.name}' already registered")
        self.tools[spec.name] = spec


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
#   Persisted assistant turn
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class AssistantTurn:
    """Persisted snapshot of one assistant message produced inside the loop."""

    content: str
    tool_calls: list[dict[str, Any]] | None
    input_tokens: int | None
    output_tokens: int | None


# ──────────────────────────────────────────────────────────────────────────
#   Model / auth resolution
# ──────────────────────────────────────────────────────────────────────────


# Default model when ``CHAT_MODEL_B2C`` / ``CHAT_MODEL_B2B`` /
# ``CHAT_MODEL`` are unset. Bare model name (no provider prefix) —
# google-genai takes plain strings.
_DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"

# Marker that ``litellm`` (legacy) used to smuggle ``thoughtSignature``
# inside the tool_call id. We decode it back when reading history rows
# that were persisted before we removed litellm, so signatures survive
# the migration boundary on existing conversations.
_THOUGHT_MARKER = "__thought__"


def strip_provider_prefix(model: str) -> str:
    """Drop legacy litellm-style provider prefixes from a model string.

    google-genai expects plain model names (``gemini-3.1-flash-lite-preview``);
    older config values may still carry ``gemini/...`` or
    ``vertex_ai/...`` from the litellm era. Strip them so callers don't
    have to migrate env vars.
    """
    for prefix in ("gemini/", "vertex_ai/", "vertex_ai_beta/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def default_b2c_model() -> str:
    raw = (
        os.getenv("CHAT_MODEL_B2C")
        or os.getenv("CHAT_MODEL")
        or _DEFAULT_MODEL
    )
    return strip_provider_prefix(raw)


def default_b2b_model() -> str:
    raw = (
        os.getenv("CHAT_MODEL_B2B")
        or os.getenv("CHAT_MODEL")
        or _DEFAULT_MODEL
    )
    return strip_provider_prefix(raw)


def default_api_key() -> str | None:
    """``CHAT_API_KEY`` is the canonical name; ``GEMINI_API_KEY`` is a
    defensive fallback so the chat keeps working if only one of the two
    is set."""
    return os.getenv("CHAT_API_KEY") or os.getenv("GEMINI_API_KEY") or None


# ──────────────────────────────────────────────────────────────────────────
#   OpenAI-shape ↔ Gemini-shape translation
# ──────────────────────────────────────────────────────────────────────────


def _split_litellm_id(raw_id: str | None) -> tuple[str, bytes | None]:
    """Decode a legacy litellm-smuggled tool_call id.

    Returns ``(clean_id, signature_bytes_or_None)``. When the id has no
    ``__thought__`` marker — i.e., it was persisted by the post-litellm
    direct path or by an older path that pre-dates signatures — we
    return the id verbatim and ``None``.
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
    function declarations. We use ``parameters_json_schema`` so we can
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
        name=..., response={...the tool's output dict, top-level})])``

    Consecutive ``tool`` rows collapse into a single ``user``-role
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
            # Pass the dict at the top level — no `{"result": ...}`
            # wrapper. Tools in this codebase always return
            # JSON-serializable dicts; the model is trained to read the
            # response payload directly. The wrapper makes the model
            # treat the data as opaque and breaks tool chaining.
            pending_tool_parts.append(
                genai_types.Part.from_function_response(
                    name=name,
                    response=parsed if isinstance(parsed, dict) else {"value": parsed},
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
                # Prefer an explicit field if persisted by the direct
                # path; fall back to the legacy ``__thought__`` smuggling.
                explicit_sig = tc.get("thought_signature")
                if isinstance(explicit_sig, str):
                    try:
                        explicit_sig = base64.b64decode(explicit_sig, validate=False)
                    except Exception:
                        explicit_sig = None
                signature = explicit_sig if explicit_sig else smuggled_sig
                args_raw = (
                    tc.get("function", {}).get("arguments")
                    or tc.get("arguments")
                    or "{}"
                )
                try:
                    args = (
                        json.loads(args_raw)
                        if isinstance(args_raw, str)
                        else (args_raw or {})
                    )
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


# ──────────────────────────────────────────────────────────────────────────
#   Context-window guard
# ──────────────────────────────────────────────────────────────────────────


# Gemini 3.1 Flash Lite and 2.5 Flash both expose ~1M input tokens. We
# truncate well before that so the model still has headroom for its
# own internal state, and so a single oversized tool result doesn't
# tip us over on the next turn.
_DEFAULT_INPUT_TOKEN_CAP = 800_000
# Skip the round-trip to ``count_tokens`` while ``contents`` is short.
# Token counting is free quota-wise but costs ~100 ms, and turn-1
# Sommelier prompts never approach the cap. Once history grows past
# this many rows it's worth measuring before every iteration.
_GUARD_MIN_CONTENT_ROWS = 12


def _is_function_call_content(content: genai_types.Content) -> bool:
    return content.role == "model" and any(
        getattr(p, "function_call", None) is not None
        for p in (content.parts or [])
    )


def _is_function_response_content(content: genai_types.Content) -> bool:
    return content.role == "user" and any(
        getattr(p, "function_response", None) is not None
        for p in (content.parts or [])
    )


def _into_blocks(
    contents: list[genai_types.Content],
) -> list[list[genai_types.Content]]:
    """Group ``contents`` into atomic blocks the truncator can drop as
    a unit. Gemini rejects an orphan ``function_call`` (without its
    matching ``function_response``) and vice versa, so when a model
    turn carries a ``function_call`` we glue the immediately following
    user turn (the ``function_response``) into the same block. Plain
    user/assistant text turns become single-row blocks.
    """
    blocks: list[list[genai_types.Content]] = []
    i = 0
    while i < len(contents):
        c = contents[i]
        if (
            _is_function_call_content(c)
            and i + 1 < len(contents)
            and _is_function_response_content(contents[i + 1])
        ):
            blocks.append([c, contents[i + 1]])
            i += 2
        else:
            blocks.append([c])
            i += 1
    return blocks


def _from_blocks(
    blocks: list[list[genai_types.Content]],
) -> list[genai_types.Content]:
    return [c for block in blocks for c in block]


async def _truncate_contents_to_fit(
    *,
    client: genai.Client,
    model: str,
    system: str,
    tool_list: list[genai_types.Tool],
    contents: list[genai_types.Content],
    cap: int = _DEFAULT_INPUT_TOKEN_CAP,
    min_rows: int = _GUARD_MIN_CONTENT_ROWS,
) -> tuple[list[genai_types.Content], int | None]:
    """Drop oldest blocks until the prompt fits under ``cap`` tokens.

    Returns ``(contents, total_tokens_after)``. ``total_tokens_after``
    is ``None`` when the guard was skipped (short history) or the
    ``count_tokens`` call itself failed — in both cases we proceed
    with the original ``contents`` and let the live call surface any
    real overflow.

    ``min_rows`` is the row threshold below which we skip the
    ``count_tokens`` round-trip entirely. Exposed as a kwarg only so
    tests can force the guard to engage with short fixtures; live
    callers always use the module default.

    Truncation strategy: walk blocks oldest-first and drop one block
    per attempt, recounting after each drop. We never drop the most
    recent block (the live user turn). If even keeping the single
    last block doesn't fit, we log and return what we have — at that
    point either the live turn itself is gigantic (an explicit user
    paste) or the model is mid-pathology, and either way truncating
    further would lie to the agent."""
    if len(contents) <= min_rows:
        return contents, None

    # Note: AI Studio's ``count_tokens`` rejects ``system_instruction``
    # and ``tools`` in ``CountTokensConfig`` (the SDK raises locally
    # before sending). We count contents-only and shrink the effective
    # cap to reserve headroom for the prefix. Sommelier system + tools
    # ≈ 18K, Business ≈ 12K — adding 40K of slack covers both with
    # plenty of margin against the 1M context window.
    try:
        response = await client.aio.models.count_tokens(
            model=model, contents=contents
        )
    except Exception as exc:  # network, auth, validation, etc.
        logger.warning("count_tokens failed; skipping guard: %s", exc)
        return contents, None

    total = response.total_tokens or 0
    if total <= cap:
        return contents, total

    logger.warning(
        "agent_loop: prompt at %d tokens > cap %d; truncating oldest blocks",
        total,
        cap,
    )
    blocks = _into_blocks(contents)
    dropped = 0
    while len(blocks) > 1:
        blocks.pop(0)
        dropped += 1
        candidate = _from_blocks(blocks)
        try:
            response = await client.aio.models.count_tokens(
                model=model, contents=candidate
            )
        except Exception as exc:
            logger.warning(
                "count_tokens during truncation failed after %d drops: %s",
                dropped,
                exc,
            )
            return candidate, None
        total = response.total_tokens or 0
        if total <= cap:
            logger.info(
                "agent_loop: truncated %d block(s); now at %d tokens",
                dropped,
                total,
            )
            return candidate, total

    final = _from_blocks(blocks)
    logger.warning(
        "agent_loop: kept only the latest block (still %d tokens, cap %d)",
        total,
        cap,
    )
    return final, total


# ──────────────────────────────────────────────────────────────────────────
#   Context caching (Gemini Cached Contents)
# ──────────────────────────────────────────────────────────────────────────


# TTL for a cache entry. Long enough that multi-turn conversations
# stay on the same cache (typical chat = 5-10 minutes, dashboard
# sessions stretch longer); short enough that idle caches don't pile
# up storage for hours after the user left.
_CACHE_TTL_SECONDS = 1800
# Safety margin subtracted from TTL when storing the local expiry —
# we'd rather re-create early than fire a request against a cache
# whose remote TTL just lapsed (race between our local clock and
# Gemini's).
_CACHE_REUSE_MARGIN_SECONDS = 60
# Skip caching when the prefix (system + tools serialized) is below
# this many characters. Gemini rejects caches below ~1024 tokens with
# a hard error; we check char-length as a cheap proxy so we don't
# bother the API for sub-min prefixes. 4000 chars ≈ 1000-1500 tokens
# on Spanish text + JSON.
_CACHE_MIN_PREFIX_CHARS = 4000
# Hard kill switch. ``AGENT_LOOP_CACHE_DISABLED=1`` in env disables
# caching at process boot; the loop falls back to inline
# ``system_instruction`` + ``tools``. Cheaper to flip than a deploy.
_CACHE_DISABLED = os.getenv("AGENT_LOOP_CACHE_DISABLED", "").lower() in {
    "1",
    "true",
    "yes",
}


@dataclass
class _CachedEntry:
    name: str
    expires_at: datetime


# Process-local map from a (model, system, tools) hash to the Gemini
# cache resource name. Survives across turns and across conversations
# that share the same system + tool surface. Reset on process restart
# (Railway deploys, dev reloads) — first request after a restart pays
# the full prefix once to repopulate.
_cached_content_registry: dict[str, _CachedEntry] = {}


def _serialize_tools_for_hash(
    tool_list: list[genai_types.Tool],
) -> list[dict[str, Any]]:
    """Stable representation of the tool surface for hashing. Stick to
    the fields that actually shape the cache (name, description,
    JSONSchema) — Tool wrappers can carry SDK-internal state we don't
    want to fingerprint."""
    out: list[dict[str, Any]] = []
    for t in tool_list:
        for fd in t.function_declarations or []:
            out.append(
                {
                    "name": fd.name,
                    "description": fd.description,
                    "parameters": fd.parameters_json_schema,
                }
            )
    return out


def _cache_key(
    model: str, system: str, tool_list: list[genai_types.Tool]
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "system": system,
            "tools": _serialize_tools_for_hash(tool_list),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _estimate_prefix_chars(
    system: str, tool_list: list[genai_types.Tool]
) -> int:
    n = len(system or "")
    for t in tool_list:
        for fd in t.function_declarations or []:
            n += len(fd.name or "") + len(fd.description or "")
            schema = fd.parameters_json_schema
            if schema is not None:
                # Cheap stringify — we only need a rough size, not a
                # canonical serialization.
                n += len(str(schema))
    return n


async def _ensure_cached_content(
    *,
    client: genai.Client,
    model: str,
    system: str,
    tool_list: list[genai_types.Tool],
    ttl_seconds: int = _CACHE_TTL_SECONDS,
    min_prefix_chars: int = _CACHE_MIN_PREFIX_CHARS,
) -> str | None:
    """Return a Gemini ``cachedContents/...`` name to reuse, or ``None``
    so the caller falls back to inline ``system_instruction`` + ``tools``.

    Best-effort: this never raises. If ``caches.create`` fails (size
    below the model minimum, transient API error, model not eligible
    for caching), we log and return ``None``. The agent loop then uses
    the inline path for that turn — correct, just more expensive.

    Caching is keyed by a hash of ``(model, system, serialized_tools)``
    so a per-user system prompt (with display name, allergies,
    wishlist) gets its own cache. The trade-off is cardinality:
    storage grows with unique system prompts within the TTL window.
    With ~30 min TTL and a small active user count this is fine; if
    we ever see cache storage become a cost line, the next move is to
    factor the per-user block out of ``system_instruction`` and into
    a leading user message instead."""
    if _CACHE_DISABLED:
        return None
    if _estimate_prefix_chars(system, tool_list) < min_prefix_chars:
        return None

    key = _cache_key(model, system, tool_list)
    now = datetime.now(timezone.utc)

    cached = _cached_content_registry.get(key)
    if cached is not None and cached.expires_at > now:
        return cached.name

    try:
        result = await client.aio.caches.create(
            model=model,
            config=genai_types.CreateCachedContentConfig(
                system_instruction=system,
                tools=tool_list,
                ttl=f"{ttl_seconds}s",
                display_name=f"agent-loop-{key[:8]}",
            ),
        )
    except Exception as exc:  # network, validation, "below minimum size"
        logger.warning(
            "agent_loop: caches.create failed; falling back to inline: %s",
            exc,
        )
        # Drop a stale entry if we had one — next request will retry.
        _cached_content_registry.pop(key, None)
        return None

    name = result.name
    if not name:
        return None

    expires_at = now + timedelta(
        seconds=max(ttl_seconds - _CACHE_REUSE_MARGIN_SECONDS, 60)
    )
    _cached_content_registry[key] = _CachedEntry(name=name, expires_at=expires_at)
    logger.info(
        "agent_loop: created cache %s (key=%s, ttl=%ds)",
        name,
        key[:8],
        ttl_seconds,
    )
    return name


def _clear_cached_content_registry() -> None:
    """Test helper. Clears the process-local registry so each test
    starts from a known empty state."""
    _cached_content_registry.clear()


# ──────────────────────────────────────────────────────────────────────────
#   Agent loop
# ──────────────────────────────────────────────────────────────────────────


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
        self.model = strip_provider_prefix(model)
        self.registry = registry
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.api_key = api_key
        # google-genai picks AI Studio when ``api_key`` is given; if we
        # ever need Vertex (e.g. preview-only model not in AI Studio's
        # catalog), this is where we'd flip ``vertexai=True`` +
        # project/location.
        self._client = (
            genai.Client(api_key=api_key) if api_key else genai.Client()
        )

    async def run(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop and yield events as they happen."""
        contents = _messages_to_contents(messages)
        tool_list = _registry_to_gemini_tools(self.registry)

        # Try to attach a Gemini Cached Content for the (model, system,
        # tools) prefix. When the cache is in use we omit
        # ``system_instruction`` and ``tools`` from each call — they
        # come from the cache, billed at ~25% of normal input cost.
        cached_name = await _ensure_cached_content(
            client=self._client,
            model=self.model,
            system=system,
            tool_list=tool_list,
        )

        for iteration in range(self.max_iterations):
            # Guard against context-window overflow. Cheap no-op when
            # history is short; drops oldest tool-call/response pairs
            # before we hand contents to the streaming call when the
            # prompt is near the cap. Always preserves the latest user
            # turn — we never lie about what the user just asked.
            contents, _ = await _truncate_contents_to_fit(
                client=self._client,
                model=self.model,
                system=system,
                tool_list=tool_list,
                contents=contents,
            )

            if cached_name is not None:
                config = genai_types.GenerateContentConfig(
                    cached_content=cached_name,
                    max_output_tokens=self.max_tokens,
                    automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                        disable=True,
                    ),
                )
            else:
                config = genai_types.GenerateContentConfig(
                    system_instruction=system,
                    tools=tool_list,
                    max_output_tokens=self.max_tokens,
                    automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                        disable=True,  # we run the loop manually
                    ),
                )

            stream_started = time.monotonic()

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
                        # what makes Vertex stop yelling about
                        # ``position N`` on follow-up turns.
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
                                    # Stored as base64 so the JSONB
                                    # column stays plain text; we decode
                                    # on the way back in
                                    # ``_messages_to_contents``.
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
                "agent_loop iter=%d tools=%d duration_ms=%d",
                iteration,
                len(tool_calls_list or []),
                int((time.monotonic() - stream_started) * 1000),
            )

            if not tool_calls_list:
                yield AgentEvent("done", None)
                return

            # Append the assistant Content verbatim — same Part objects
            # we just streamed, including ``thought_signature``s.
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
                            response=err,
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
                            response=err,
                        )
                    )
                    continue

                yield AgentEvent(
                    "tool_call_start",
                    {"id": tool_id, "name": tool_name, "input": parsed_args},
                )

                try:
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
                        response=output if isinstance(output, dict) else {"value": output},
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
    "AgentEvent",
    "AgentLoop",
    "AssistantTurn",
    "ToolFunc",
    "ToolRegistry",
    "ToolSpec",
    "default_api_key",
    "default_b2b_model",
    "default_b2c_model",
]
