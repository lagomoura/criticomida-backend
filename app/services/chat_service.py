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
from app.services.chat.prompts.loader import build_user_block, load_agent_prompt
from app.services.chat.tools.registry import build_registry
from app.services.embeddings_service import embed_query
from app.services.taste_profile_service import get_taste_profile

logger = logging.getLogger(__name__)


HISTORY_TURNS = 12  # last N messages we feed to the model


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


async def _load_history(
    db: AsyncSession, conversation_id: uuid.UUID
) -> list[dict[str, Any]]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.asc())
    )
    rows = list((await db.execute(stmt)).scalars().all())
    # Drop the oldest if we exceed the cap; always keep tool/result
    # pairs together (they reference each other by ID).
    rows = rows[-HISTORY_TURNS:]

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
    await db.flush()

    # ── build context ─────────────────────────────────────────────────────
    profile = (
        await get_taste_profile(db, user.id) if user is not None else None
    )
    system_prompt = load_agent_prompt(conversation.agent)
    user_block = build_user_block(user, profile)
    if user_block:
        system_prompt = f"{system_prompt}\n\n{user_block}"

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
