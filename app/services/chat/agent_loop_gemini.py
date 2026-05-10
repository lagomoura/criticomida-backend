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

Fases:

- F1 — andamiaje (este archivo, esqueleto + types compartidos).
- F2 — core loop (translation de tools, streaming, tool execution,
  multi-turn con thoughtSignature). Aún no implementado.
- F3 — persistencia (``_load_gemini_history`` en chat_service).
- F4 — wiring del flag en chat_service.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

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

    async def run(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop and yield events as they happen.

        ``messages`` follows the same OpenAI-shaped contract the litellm
        path uses (so ``chat_service._load_history`` keeps working);
        the implementation will translate to/from Gemini's ``Content`` /
        ``Part`` shape internally.
        """
        # Phase 2 territory.
        raise NotImplementedError(
            "agent_loop_gemini.AgentLoop.run is not implemented yet "
            "(Phase 2). Set GEMINI_DIRECT=false to fall back to the "
            "litellm path."
        )
        # Make this an async generator even when raising, so callers
        # that ``async for`` over it get a typed iterator back.
        yield AgentEvent("done", None)  # pragma: no cover


__all__ = [
    "AgentLoop",
    "AssistantTurn",
    "default_b2c_model_gemini",
    "default_b2b_model_gemini",
    "default_api_key_gemini",
]
