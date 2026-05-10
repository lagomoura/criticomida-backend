"""Thin orchestrator around ``AgentLoop``.

Responsibilities:

- Resolve the right system prompt + taste-profile addendum for the
  active conversation.
- Build the ``ToolRegistry`` bound to the request session and user.
- Hydrate the message list from ``chat_messages`` (last N turns).
- Stream the loop's events to the caller, persisting each assistant
  message + tool call/result row as they happen.

Persistence happens *during* the stream so an aborted connection still
leaves a coherent transcript on disk — important for Business audits.
History hydration re-serializes tool results back into the OpenAI shape
because the model expects a string ``content`` on ``role='tool'`` rows,
not the original dict we persisted.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import (
    ChatAgent,
    ChatConversation,
    ChatMessage,
)
from app.models.user import User
from app.services.chat.agent_loop import (
    AgentEvent,
    AgentLoop,
    AssistantTurn,
    default_api_key,
    default_b2b_model,
    default_b2c_model,
)
from app.services.chat.preference_intent import detect_preference_intent
from app.services.chat.user_preference_intent import (
    detect_user_preference_intent,
)
from app.services.chat.prompts.loader import build_user_block, load_agent_prompt
from app.services.chat.tools.registry import build_registry
from app.services.chat_title_service import schedule_generate_title
from app.services.embeddings_service import embed_query
from app.services.owner_chat_preferences_service import (
    get_chat_preferences,
    render_preferences_block,
    upsert_chat_preference,
)
from app.services.user_chat_preferences_service import (
    get_user_chat_preferences,
    render_user_preferences_block,
    upsert_user_chat_preference,
)
from app.services.taste_profile_service import get_taste_profile

logger = logging.getLogger(__name__)


HISTORY_TURNS = 12  # last N messages we feed to the model

TITLE_MAX_LEN = 60  # chars; trimmed conversation label for the history panel


def _make_title_from_user_message(text: str, max_len: int = TITLE_MAX_LEN) -> str:
    """First-message-as-title heuristic.

    Cheap and deterministic: collapses whitespace and truncates with
    an ellipsis. Good enough for the history panel — the owner sees
    *what they asked*, which is the most identifiable label until we
    layer an LLM-generated title on top.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


# ──────────────────────────────────────────────────────────────────────────
#   Conversation helpers
# ──────────────────────────────────────────────────────────────────────────


async def get_or_create_conversation(
    db: AsyncSession,
    *,
    conversation_id: uuid.UUID | None,
    user: User | None,
    agent: ChatAgent,
    restaurant_scope_id: uuid.UUID | None = None,
) -> ChatConversation:
    if conversation_id is not None:
        stmt = select(ChatConversation).where(
            ChatConversation.id == conversation_id
        )
        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            # Caller-provided IDs from a different user are a privacy
            # leak: refuse and start fresh instead.
            same_owner = (
                existing.user_id == (user.id if user else None)
            )
            if same_owner:
                return existing

    convo = ChatConversation(
        id=uuid.uuid4(),
        user_id=user.id if user else None,
        agent=agent,
        restaurant_scope_id=restaurant_scope_id,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
    )
    db.add(convo)
    await db.flush()
    return convo


def _sanitize_for_strict_turn_grammar(
    rows: list[ChatMessage],
) -> list[ChatMessage]:
    """Vertex Gemini enforces strict turn grammar — a ``function_call``
    (= assistant message with ``tool_calls``) must follow either a
    ``user`` or a ``function_response`` (= ``role='tool'``) turn, never
    an assistant text. Two cleanups defend the slice:

    1. **Leading non-user rows** — when the ``[-N:]`` slice cuts mid
       tool sequence, the head is an orphan ``tool`` or
       ``assistant(tool_calls)`` row. Drop until the first ``user``.
    2. **Trailing orphan ``assistant(tool_calls)``** — happens when a
       previous turn crashed mid-stream after persisting the assistant
       row but before the tool responses landed. Appending the next
       user message would yield ``function_call → user``, invalid.
       Drop the orphan assistant.

    Both shapes were valid for OpenAI and Google AI Studio direct
    (which is why this only surfaced after switching to a Vertex Beta
    preview model). Defensive sanitisation is provider-agnostic — we
    don't want to depend on the upstream tolerance.
    """
    while rows and rows[0].role != "user":
        rows = rows[1:]

    if rows:
        last = rows[-1]
        if last.role == "assistant" and last.tool_calls:
            rows = rows[:-1]

    return rows


async def _load_history(
    db: AsyncSession, conversation_id: uuid.UUID
) -> list[dict[str, Any]]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.asc())
    )
    rows = list((await db.execute(stmt)).scalars().all())
    rows = rows[-HISTORY_TURNS:]
    rows = _sanitize_for_strict_turn_grammar(rows)

    out: list[dict[str, Any]] = []
    for r in rows:
        if r.role == "assistant":
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": r.content,
            }
            if r.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc.get("arguments") or "{}",
                        },
                    }
                    for tc in r.tool_calls
                ]
            out.append(entry)
        elif r.role == "tool":
            payload = r.tool_result or {}
            content = payload.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, default=str)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": payload.get("id", ""),
                    "content": content,
                }
            )
        else:  # user
            out.append({"role": "user", "content": r.content or ""})
    return out


# ──────────────────────────────────────────────────────────────────────────
#   Streaming entry point
# ──────────────────────────────────────────────────────────────────────────


async def stream_chat(
    db: AsyncSession,
    *,
    conversation: ChatConversation,
    user: User | None,
    user_message: str,
) -> AsyncIterator[AgentEvent]:
    """Run one turn of the chat loop, persisting messages as we go."""

    # ── persist the incoming user message ─────────────────────────────────
    user_row = ChatMessage(
        conversation_id=conversation.id,
        role="user",
        content=user_message,
    )
    db.add(user_row)
    conversation.last_message_at = datetime.now(timezone.utc)
    # Auto-title from the first user message — cheap and deterministic.
    # The history panel shows it as the conversation label so the owner
    # sees *what they asked* instead of "Untitled". Once an LLM-based
    # titler is worth the cost we can layer one on top, gated by
    # title still being None / a "[draft]" marker.
    is_first_user_message = not conversation.title
    if is_first_user_message:
        conversation.title = _make_title_from_user_message(user_message)
    await db.flush()

    # ── deterministic preference middleware (Business agent only) ─────────
    # Layer 1 of the 3-layer defence against the LLM dropping
    # ``update_owner_preferences`` calls. See
    # ``app/services/chat/preference_intent.py`` for rationale and
    # ``docs/chatbot.md`` for the full picture.
    prefs_just_persisted: dict[str, str | None] | None = None
    if (
        conversation.agent == ChatAgent.business
        and user is not None
        and conversation.restaurant_scope_id is not None
    ):
        intent = detect_preference_intent(user_message)
        if intent:
            saved = await upsert_chat_preference(
                db,
                user_id=user.id,
                restaurant_id=conversation.restaurant_scope_id,
                tone_preference=intent.get("tone"),
                language_preference=intent.get("language"),
            )
            prefs_just_persisted = {
                "tone": saved.tone_preference,
                "language": saved.language_preference,
            }
            logger.info(
                "preference_intent.persisted user=%s restaurant=%s intent=%s",
                user.id,
                conversation.restaurant_scope_id,
                dict(intent),
            )

    # Same defence layer for the Sommelier (B2C). Catches "siempre
    # respondé en inglés" / "de ahora en más hablame corto" before
    # the LLM gets a chance to confirm verbally without persisting.
    if (
        conversation.agent == ChatAgent.sommelier
        and user is not None
    ):
        user_intent = detect_user_preference_intent(user_message)
        if user_intent:
            saved_user = await upsert_user_chat_preference(
                db,
                user_id=user.id,
                language_preference=user_intent.get("language"),
                response_style=user_intent.get("response_style"),
            )
            prefs_just_persisted = {
                "language": saved_user.language_preference,
                "response_style": saved_user.response_style,
            }
            logger.info(
                "user_preference_intent.persisted user=%s intent=%s",
                user.id,
                dict(user_intent),
            )

    # ── build context ─────────────────────────────────────────────────────
    profile = (
        await get_taste_profile(db, user.id) if user is not None else None
    )
    system_prompt = load_agent_prompt(conversation.agent)
    user_block = await build_user_block(db, user, profile)
    if user_block:
        system_prompt = f"{system_prompt}\n\n{user_block}"

    # Business agent: append the per-restaurant chat preferences (tone,
    # language, KPI focus) the owner has tweaked in past sessions. Sin
    # fila → bloque omitido y el agente cae a defaults del prompt.
    if (
        conversation.agent == ChatAgent.business
        and user is not None
        and conversation.restaurant_scope_id is not None
    ):
        prefs = await get_chat_preferences(
            db,
            user_id=user.id,
            restaurant_id=conversation.restaurant_scope_id,
        )
        prefs_block = render_preferences_block(prefs)
        if prefs_block:
            system_prompt = f"{system_prompt}\n\n{prefs_block}"

    # Sommelier (B2C): append per-comensal chat preferences (language +
    # response style) so the agent inherits past sessions' choices.
    # Same shape as the Business injection; sin fila → bloque omitido.
    if conversation.agent == ChatAgent.sommelier and user is not None:
        user_prefs = await get_user_chat_preferences(db, user_id=user.id)
        user_prefs_block = render_user_preferences_block(user_prefs)
        if user_prefs_block:
            system_prompt = f"{system_prompt}\n\n{user_prefs_block}"

    # Transient note (this turn only) — keeps the LLM from re-calling
    # ``update_owner_preferences`` after the regex middleware already
    # saved the same intent. Idempotent re-writes are harmless but the
    # confirmation phrasing is cleaner when the model knows it's done.
    if prefs_just_persisted:
        summary = ", ".join(
            f"{k}={v}" for k, v in prefs_just_persisted.items() if v
        )
        system_prompt = (
            f"{system_prompt}\n\n# Persisted in this turn\n"
            "A deterministic preprocessor already saved the owner's "
            f"explicit preference change ({summary}). Do NOT call "
            "`update_owner_preferences` again in this turn — confirm "
            "the change in one short sentence and continue with the "
            "rest of the owner's message if applicable."
        )

    history = await _load_history(db, conversation.id)
    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    registry = build_registry(
        agent=conversation.agent,
        db=db,
        user_id=user.id if user else None,
        embed_query=embed_query,
        restaurant_scope_id=(
            str(conversation.restaurant_scope_id)
            if conversation.restaurant_scope_id
            else None
        ),
        conversation_id=conversation.id,
    )

    model = (
        default_b2b_model()
        if conversation.agent == ChatAgent.business
        else default_b2c_model()
    )
    loop = AgentLoop(
        model=model,
        registry=registry,
        api_key=default_api_key(),
    )

    # Buffer the most recent assistant turn so we can attach tool result
    # rows to it before it gets persisted on the next iteration.
    last_assistant_id: uuid.UUID | None = None

    async for event in loop.run(system=system_prompt, messages=messages):
        if event.type == "message_complete":
            turn: AssistantTurn = event.data
            row = ChatMessage(
                conversation_id=conversation.id,
                role="assistant",
                content=turn.content or None,
                tool_calls=[
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc.get("arguments") or "",
                        # Optional: only the Gemini-direct path sets
                        # this. Carrying it forward into the DB lets the
                        # next turn rehydrate the full Part with
                        # signature so Vertex doesn't reject the
                        # functionCall on history replay.
                        **(
                            {"thought_signature": tc["thought_signature"]}
                            if tc.get("thought_signature")
                            else {}
                        ),
                    }
                    for tc in (turn.tool_calls or [])
                ]
                or None,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
            )
            db.add(row)
            await db.flush()
            last_assistant_id = row.id
            conversation.last_message_at = datetime.now(timezone.utc)
            await db.flush()
            # Don't surface message_complete to the FE: the FE only
            # cares about deltas + tool events + done.
            continue

        if event.type == "tool_call_result":
            payload = event.data
            tool_row = ChatMessage(
                conversation_id=conversation.id,
                role="tool",
                tool_result={
                    "id": payload["id"],
                    "name": payload["name"],
                    "content": payload["output"],
                    "is_error": payload["is_error"],
                    "linked_assistant_id": (
                        str(last_assistant_id) if last_assistant_id else None
                    ),
                },
            )
            db.add(tool_row)
            await db.flush()

        yield event

    await db.commit()

    # Layered title: the heuristic in ``last_message_at == started_at``
    # already gave the panel something to show. Now that the assistant
    # turn is persisted too, fire the LLM titler in the background so
    # the panel swaps to a concise theme-style title in ~3-5 s. Only
    # on the first user turn — later turns don't need re-titling and
    # would cost tokens for nothing.
    if is_first_user_message:
        schedule_generate_title(conversation.id)
