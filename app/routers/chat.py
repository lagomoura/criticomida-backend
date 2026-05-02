"""Chat endpoints.

- ``POST /api/chat/stream`` — Server-Sent Events. The FE consumes deltas
  in real time and renders text/cards as they arrive.
- ``POST /api/chat/conversations`` — open a new conversation explicitly.
- ``GET /api/chat/conversations/me`` — list the current user's chats.
- ``GET /api/chat/conversations/{id}/messages`` — paginated transcript.
- ``DELETE /api/chat/conversations/{id}`` — "olvidame" / GDPR-style
  delete.

The legacy ``POST /api/chat`` non-streaming endpoint is kept around for
one release so existing widget builds don't break, but it's marked
deprecated and proxies to the streaming flow internally.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, get_current_user_optional
from app.models.chat import (
    ChatAgent,
    ChatConversation,
    ChatMessage,
)
from app.models.user import User
from app.services.chat_service import (
    get_or_create_conversation,
    stream_chat,
)
from app.services.claim_service import assert_verified_owner


router = APIRouter(prefix="/api/chat", tags=["chat"])


# ──────────────────────────────────────────────────────────────────────────
#   Schemas
# ──────────────────────────────────────────────────────────────────────────


class StreamChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    agent: ChatAgent = ChatAgent.sommelier
    restaurant_scope_id: uuid.UUID | None = None


class ConversationCreate(BaseModel):
    agent: ChatAgent = ChatAgent.sommelier
    title: str | None = Field(default=None, max_length=200)
    restaurant_scope_id: uuid.UUID | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    agent: ChatAgent
    title: str | None
    started_at: datetime
    last_message_at: datetime
    restaurant_scope_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_result: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ──────────────────────────────────────────────────────────────────────────
#   Streaming endpoint
# ──────────────────────────────────────────────────────────────────────────


def _sse_pack(event_type: str, data: Any) -> str:
    """Encode an SSE frame. We send the type as `event:` so the FE can
    use ``addEventListener`` instead of parsing payloads manually."""
    payload = json.dumps({"type": event_type, "data": data}, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


@router.post("/stream")
async def stream_endpoint(
    body: StreamChatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """Run a single chat turn and stream events as SSE."""

    # Business agent requires an authenticated owner (or an admin) of
    # the scoped restaurant. ``assert_verified_owner`` already bypasses
    # for ``UserRole.admin`` so support / moderation can debug from any
    # restaurant's owner panel without a claim.
    if body.agent == ChatAgent.business:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Business agent requires authentication.",
            )
        if body.restaurant_scope_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Business agent requires restaurant_scope_id.",
            )
        await assert_verified_owner(
            db, user=user, restaurant_id=body.restaurant_scope_id
        )

    convo = await get_or_create_conversation(
        db,
        conversation_id=body.conversation_id,
        user=user,
        agent=body.agent,
        restaurant_scope_id=body.restaurant_scope_id,
    )

    async def event_stream():
        # Always tell the FE the conversation id first, even if the
        # caller didn't pass one. Lets the FE persist it for the next
        # request.
        yield _sse_pack(
            "conversation",
            {"id": str(convo.id), "agent": convo.agent.value},
        )

        try:
            async for event in stream_chat(
                db,
                conversation=convo,
                user=user,
                user_message=body.message,
            ):
                yield _sse_pack(event.type, event.data)
        except Exception as exc:  # noqa: BLE001
            yield _sse_pack("error", {"message": str(exc)})
            return
        finally:
            yield _sse_pack("done", None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
            "Connection": "keep-alive",
        },
    )


# ──────────────────────────────────────────────────────────────────────────
#   Conversation CRUD
# ──────────────────────────────────────────────────────────────────────────


@router.post(
    "/conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: ConversationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> ChatConversation:
    convo = ChatConversation(
        user_id=user.id,
        agent=body.agent,
        title=body.title,
        restaurant_scope_id=body.restaurant_scope_id,
    )
    db.add(convo)
    await db.flush()
    return convo


@router.get(
    "/conversations/me", response_model=list[ConversationOut]
)
async def list_my_conversations(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ChatConversation]:
    stmt = (
        select(ChatConversation)
        .where(ChatConversation.user_id == user.id)
        .order_by(ChatConversation.last_message_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
async def list_messages(
    conversation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ChatMessage]:
    convo = (
        await db.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalars().first()
    if convo is None or convo.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")

    stmt = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    convo = (
        await db.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalars().first()
    if convo is None or convo.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    await db.delete(convo)
    await db.flush()
    return None


# ──────────────────────────────────────────────────────────────────────────
#   Legacy non-streaming endpoint (deprecated, removed in next release)
# ──────────────────────────────────────────────────────────────────────────


class LegacyChatMessage(BaseModel):
    role: str
    content: str


class LegacyChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[LegacyChatMessage] = Field(default_factory=list)


class LegacyChatResponse(BaseModel):
    response: str


@router.post("", response_model=LegacyChatResponse, deprecated=True)
async def legacy_chat(
    body: LegacyChatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> LegacyChatResponse:
    """Deprecated. Drains the streaming loop and returns the final text.

    History is ignored — the new flow stores transcripts itself. Kept
    around for one release so old widget builds don't 404.
    """
    convo = await get_or_create_conversation(
        db,
        conversation_id=None,
        user=user,
        agent=ChatAgent.sommelier,
    )

    full_text: list[str] = []
    async for event in stream_chat(
        db,
        conversation=convo,
        user=user,
        user_message=body.message,
    ):
        if event.type == "text_delta":
            full_text.append(event.data)
        elif event.type == "error":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(event.data),
            )

    return LegacyChatResponse(response="".join(full_text))
