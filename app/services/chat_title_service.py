"""Auto-titler for chat conversations via Gemini Flash.

The conversation history panel shows ``conversation.title`` next to
each row. ``chat_service.stream_chat`` plants a heuristic title from
the first user message — cheap and instant, but reads like the user's
prompt rather than the conversation's *theme*. This service layers a
short LLM-generated title on top so the panel becomes scannable
("Subir rating del fideo" instead of the full first message).

Design mirrors ``sentiment_service`` deliberately:

- ``generate_conversation_title(messages)`` — pure call to Gemini.
- ``analyze_and_persist_title(db, conversation_id)`` — load, generate,
  write. No-op when the conversation already has a non-heuristic
  title (we never overwrite something the human (or a future title
  editor) put there on purpose).
- ``schedule_generate_title(conversation_id)`` — fire-and-forget
  wrapper that opens its own session.

Why Gemini Flash and not the chat agent's model: Flash is cheap, JSON-
mode is reliable, and the title generation isn't on the user's
critical response path. ``thinking_budget=0`` is mandatory — without
it Flash 2.5 truncates short JSON outputs (see memory
``feedback_gemini_thinking``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models.chat import ChatConversation, ChatMessage

logger = logging.getLogger(__name__)


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
_TITLE_MODEL = "gemini-2.5-flash"
_REQUEST_TIMEOUT = 12.0
# How many of the conversation's leading messages we feed to the
# titler. The first user turn alone is usually enough; the assistant's
# reply (when available) helps disambiguate ("preguntó por la
# competencia, no por su propio rating"). Anything past that is
# noise and bumps the prompt cost without changing the title.
_MAX_MESSAGES = 4
# Per-message text cap. Reviews / questions can be long; the titler
# only needs the gist. Hard cap keeps prompt size predictable.
_MAX_CHARS_PER_MESSAGE = 600
# Hard cap on the generated title. The DB column is ``String(200)``
# but the panel layout starts to wrap past ~70 chars.
_TITLE_MAX_LEN = 80


_SYSTEM_INSTRUCTION = """Sos un titulador de conversaciones para el panel de historial de un asistente analítico gastronómico.

Recibís los primeros mensajes de una conversación (rol del usuario y del asistente) y devolvés UN título corto que describa de qué se trata.

Reglas:
- 4 a 8 palabras. Sin punto final.
- Mismo idioma que el primer mensaje del usuario (es / en / pt).
- Tono neutro: describí el tema, no parafrasees el saludo. ❌ "Hola, ¿cómo estás?" ✅ "Saludo del owner sin contexto"
- Sin signos de pregunta. Si el usuario pregunta algo, convertí la pregunta a un sustantivo. ❌ "¿Por qué bajó mi rating?" ✅ "Caída de rating del mes"
- Sin emojis, sin comillas, sin prefijos como "Tema:" o "Conversación sobre…".
- Devolvé un JSON válido con esta forma exacta:

{
  "title": "..."
}

Devolvé el JSON pelado, sin texto extra."""


_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
    },
    "required": ["title"],
}


async def generate_conversation_title(
    messages: list[tuple[str, str]],
) -> str | None:
    """Classify a conversation into a short title.

    ``messages`` is a list of ``(role, content)`` tuples in
    chronological order. Returns ``None`` when Gemini is unconfigured,
    the response is malformed, or the title is empty after cleanup.
    """
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    if not messages:
        return None

    parts: list[str] = []
    for role, content in messages[:_MAX_MESSAGES]:
        cleaned = (content or "").strip()
        if not cleaned:
            continue
        if len(cleaned) > _MAX_CHARS_PER_MESSAGE:
            cleaned = cleaned[:_MAX_CHARS_PER_MESSAGE].rstrip() + "…"
        parts.append(f"[{role}]\n{cleaned}")
    if not parts:
        return None
    user_prompt = "\n\n".join(parts)

    payload: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generation_config": {
            "response_mime_type": "application/json",
            "response_schema": _SCHEMA,
            "temperature": 0.2,
            # Mandatory for Flash 2.5 JSON-mode short outputs — see
            # ``feedback_gemini_thinking``.
            "thinking_config": {"thinking_budget": 0},
            "max_output_tokens": 64,
        },
    }
    url = f"{_GEMINI_BASE}/models/{_TITLE_MODEL}:generateContent"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(url, params={"key": key}, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        logger.warning("gemini chat-title call failed: %s", exc)
        return None

    try:
        first_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        logger.warning("gemini chat-title response shape unexpected: %s", exc)
        return None

    try:
        raw = json.loads(first_text)
    except ValueError:
        logger.warning(
            "gemini chat-title returned unparseable JSON (tail=%r)",
            first_text[-80:] if first_text else "",
        )
        return None

    title = raw.get("title")
    if not isinstance(title, str):
        return None
    title = " ".join(title.split())  # collapse internal whitespace
    title = title.strip().strip("\"'“”")
    if not title:
        return None
    if len(title) > _TITLE_MAX_LEN:
        title = title[: _TITLE_MAX_LEN - 1].rstrip() + "…"
    return title


async def analyze_and_persist_title(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    overwrite_heuristic: bool = True,
) -> None:
    """Generate a title and persist it on the conversation row.

    Caller commits. By default we overwrite the heuristic title that
    ``chat_service.stream_chat`` writes on the first user turn — the
    heuristic is a stop-gap that gives the panel something to show
    until this LLM call lands. Pass ``overwrite_heuristic=False`` to
    only fill in titles that are still null.
    """
    convo = (
        await db.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalar_one_or_none()
    if convo is None:
        return

    if not overwrite_heuristic and convo.title:
        return

    rows = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(_MAX_MESSAGES)
        )
    ).scalars().all()

    messages: list[tuple[str, str]] = []
    for row in rows:
        if row.role not in ("user", "assistant"):
            continue
        content = row.content or ""
        if not content.strip():
            continue
        messages.append((row.role, content))

    title = await generate_conversation_title(messages)
    if title is None:
        return
    convo.title = title


def schedule_generate_title(conversation_id: uuid.UUID) -> None:
    """Fire-and-forget invocation safe to call from the streaming
    handler. Opens its own session so the request session can close
    cleanly. Failures log and are swallowed — a missing LLM title is
    never worth interrupting the user's chat reply."""

    async def _run() -> None:
        try:
            async with async_session() as db:
                try:
                    await analyze_and_persist_title(db, conversation_id)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except Exception:
            logger.exception("schedule_generate_title failed")

    asyncio.create_task(_run())
