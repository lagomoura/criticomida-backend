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

# NB: deliberately NOT using ``from __future__ import annotations`` —
# bajo @limiter.limit (slowapi 0.1.9) + FastAPI 0.115.6, el wrapper
# evalúa annotations contra su propio __globals__ y los Annotated[..., Depends()]
# quedan como ForwardRef sin resolver → FastAPI degrada los params a query
# y devuelve 422 'Field required' (loc=query.body/db/user). Mismo fix
# que ghostwriter.py (commit b39474d). Local (--reload) lo enmascara;
# Railway (--workers 2) lo dispara en cada init de worker.

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user, get_current_user_optional
from app.middleware.rate_limit import CHAT_STREAM_LIMIT, limiter
from app.models.chat import (
    ChatAgent,
    ChatConversation,
    ChatMessage,
    TastePillar,
)
from app.models.user import User, UserRole
from app.services.chat_service import (
    get_or_create_conversation,
    stream_chat,
)
from app.services.claim_service import assert_verified_owner
from app.services.sommelier_recall_service import (
    dismiss_pending_recall,
    get_pending_recalls,
)
from app.services.taste_profile_service import get_taste_profile


logger = logging.getLogger(__name__)

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
    # Set when the conversation has been soft-deleted. The FE uses
    # this to render archived rows in a muted style and offer a
    # "Restaurar" action when the "Show archived" toggle is on.
    archived_at: datetime | None = None

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    tool_result: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SommelierPreviewUser(BaseModel):
    """Bare-minimum identity payload for the Sommelier empty state.

    The empty-state widget only renders the display name + handle;
    everything else (taste profile, wishlist) ships in its own
    nested fields. We keep this lean on purpose — the response is
    fetched on every chat drawer open, so it has to be cheap.
    """

    display_name: str
    handle: str | None


class SommelierPreviewProfile(BaseModel):
    """Serialised view of ``UserTasteProfile`` for the empty state.

    The label fields (``dominant_pillar_label``) are pre-translated
    Spanish strings so the FE doesn't have to maintain its own
    enum→label table; the FE shows them as-is when the locale is es.
    For en/pt, the FE has its own translation keys keyed off the
    enum value. Keep both around — losing either breaks one path.
    """

    dominant_pillar: str | None
    dominant_pillar_label: str | None
    top_neighborhoods: list[str]
    top_categories: list[str]
    favorite_tags: list[str]
    allergies: list[str]
    avg_price_band: str | None


class SommelierPreviewPendingRecall(BaseModel):
    """One row of the Post-visit Bridge (B) section of the empty state.

    Surfaces dishes the Sommelier recommended in the last 14 days that
    the diner hasn't reviewed yet. The FE renders a card per item with
    a CTA that points at the compose form pre-filled with ``dish_id``
    — same destination as the in-app notification (D2), so the two
    surfaces stay coherent.
    """

    dish_id: uuid.UUID
    dish_name: str
    cover_image_url: str | None
    restaurant_name: str
    restaurant_slug: str | None
    recommended_at: datetime


class SommelierPreviewOut(BaseModel):
    """Payload for ``GET /api/chat/sommelier/preview``.

    ``user`` and ``profile`` are nullable on purpose:

    - ``user is None`` — anonymous visitor; FE shows the sign-in
      invitation + generic starters.
    - ``profile is None`` (with ``user`` present) — logged-in user
      who hasn't reviewed enough dishes for the aggregator to infer
      preferences yet; FE shows a name greeting + generic starters
      and skips the "Te conocemos así" chip.

    ``pending_recalls`` defaults to ``[]`` so the FE never has to
    null-check the array. Anonymous callers always receive an empty
    list — there's no identity to look recalls up against.
    """

    user: SommelierPreviewUser | None
    profile: SommelierPreviewProfile | None
    pending_recalls: list[SommelierPreviewPendingRecall] = []


# ──────────────────────────────────────────────────────────────────────────
#   Streaming endpoint
# ──────────────────────────────────────────────────────────────────────────


def _sse_pack(event_type: str, data: Any) -> str:
    """Encode an SSE frame. We send the type as `event:` so the FE can
    use ``addEventListener`` instead of parsing payloads manually."""
    payload = json.dumps({"type": event_type, "data": data}, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


@router.post("/stream")
@limiter.limit(CHAT_STREAM_LIMIT)
async def stream_endpoint(
    request: Request,
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
            # ``private`` blocks any shared proxy from caching this
            # response; ``no-store`` makes intent explicit even though
            # SSE wouldn't be cached in practice. Keeps the per-user
            # transcript out of any well-meaning intermediary.
            "Cache-Control": "private, no-store, no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering
            "Connection": "keep-alive",
        },
    )


# ──────────────────────────────────────────────────────────────────────────
#   Sommelier empty-state preview
# ──────────────────────────────────────────────────────────────────────────


_PILLAR_LABEL_ES: dict[TastePillar, str] = {
    TastePillar.presentation: "presentación",
    TastePillar.execution: "ejecución técnica",
    TastePillar.value_prop: "costo/beneficio",
}


@router.get("/sommelier/preview", response_model=SommelierPreviewOut)
async def sommelier_preview(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> SommelierPreviewOut:
    """Lightweight preview the FE consumes when the Sommelier drawer
    opens with no messages yet.

    Returns the comensal's display name + handle and a serialised view
    of their ``UserTasteProfile``. Both fields are nullable so the same
    endpoint serves anonymous visitors and freshly-registered users
    (who haven't reviewed enough dishes to get a profile inferred yet).

    The endpoint is intentionally NOT auth-required: the FE call
    happens before any chat turn, so failing on a missing token would
    block the empty state for visitors. Anonymous callers just get
    ``{user: null, profile: null}`` and the FE renders the sign-in
    invitation.
    """
    if user is None:
        return SommelierPreviewOut(user=None, profile=None, pending_recalls=[])

    profile = await get_taste_profile(db, user.id)
    pending = await get_pending_recalls(db, user_id=user.id)
    profile_out: SommelierPreviewProfile | None = None
    if profile is not None:
        profile_out = SommelierPreviewProfile(
            dominant_pillar=(
                profile.dominant_pillar.value
                if profile.dominant_pillar is not None
                else None
            ),
            dominant_pillar_label=(
                _PILLAR_LABEL_ES[profile.dominant_pillar]
                if profile.dominant_pillar is not None
                else None
            ),
            top_neighborhoods=list(profile.top_neighborhoods or []),
            top_categories=list(profile.top_categories or []),
            favorite_tags=list(profile.favorite_tags or []),
            allergies=list(profile.allergies or []),
            avg_price_band=(
                profile.avg_price_band.value
                if profile.avg_price_band is not None
                else None
            ),
        )

    return SommelierPreviewOut(
        user=SommelierPreviewUser(
            display_name=user.display_name,
            handle=user.handle,
        ),
        profile=profile_out,
        pending_recalls=[
            SommelierPreviewPendingRecall(
                dish_id=item.dish_id,
                dish_name=item.dish_name,
                cover_image_url=item.cover_image_url,
                restaurant_name=item.restaurant_name,
                restaurant_slug=item.restaurant_slug,
                recommended_at=item.recommended_at,
            )
            for item in pending
        ],
    )


@router.post(
    "/sommelier/recalls/{dish_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_recall(
    dish_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Diner clicked "X" on a Post-visit Bridge card. The dish stops
    surfacing in the empty-state pending-recalls section permanently,
    even if the Sommelier re-recommends it later.

    Idempotent: the underlying INSERT uses ``ON CONFLICT DO NOTHING``,
    so repeated taps from a flaky network are silent no-ops. Auth
    required — anonymous callers can't dismiss anything because
    anonymous callers don't see the section to begin with.
    """
    await dismiss_pending_recall(db, user_id=user.id, dish_id=dish_id)
    await db.commit()


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
    agent: ChatAgent | None = Query(
        default=None,
        description=(
            "Filter to conversations of one agent (sommelier / "
            "ghostwriter / business). Used by the Business chat's "
            "history panel to scope to that single agent."
        ),
    ),
    restaurant_scope_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Filter to conversations bound to a specific restaurant. "
            "Required by the Business chat to keep a per-venue history."
        ),
    ),
    include_archived: bool = Query(
        default=False,
        description=(
            "When false (default), archived conversations are hidden. "
            "Set true to opt back in (e.g. 'Show archived' toggle in the "
            "history panel)."
        ),
    ),
) -> list[ChatConversation]:
    stmt = (
        select(ChatConversation)
        .where(ChatConversation.user_id == user.id)
        .order_by(ChatConversation.last_message_at.desc())
        .limit(limit)
    )
    if agent is not None:
        stmt = stmt.where(ChatConversation.agent == agent)
    if restaurant_scope_id is not None:
        stmt = stmt.where(
            ChatConversation.restaurant_scope_id == restaurant_scope_id
        )
    # Soft-delete: archived conversations are hidden by default. Pass
    # ``include_archived=true`` from the FE only when the owner
    # explicitly toggles "show archived".
    if not include_archived:
        stmt = stmt.where(ChatConversation.archived_at.is_(None))
    return list((await db.execute(stmt)).scalars().all())


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
async def list_messages(
    conversation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=200),
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
    response_model=None,
)
async def archive_conversation(
    conversation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Soft-delete: marca la conversación como archivada.

    Antes esto hacía hard-delete; lo cambiamos a soft para conservar el
    contenido analítico (especialmente para el agente Business). Una
    conversación archivada no aparece en ``list_my_conversations`` por
    default. Para borrado físico (GDPR) habrá un endpoint admin
    separado en el futuro.

    Idempotente: archivar dos veces no rompe nada.
    """
    convo = (
        await db.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalars().first()
    if convo is None or convo.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    if convo.archived_at is None:
        convo.archived_at = datetime.now(timezone.utc)
        await db.flush()
    return None


@router.delete(
    "/conversations/{conversation_id}/permanent",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def hard_delete_conversation(
    conversation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Hard-delete a conversation and all its messages.

    Distinct from ``archive_conversation`` (which is the soft-delete
    every regular user can do): this endpoint actually removes rows
    from the DB and is the right primitive for a GDPR / right-to-be
    -forgotten flow.

    Authorisation: the conversation's own owner, or an admin acting
    on their behalf (support / GDPR ops). Without that gate, an
    admin-shaped role drift could quietly wipe other users' data.

    The action is logged with structured fields (who, what, when —
    no message content) so we have a paper trail for compliance
    audits without hauling the deleted text along.
    """
    convo = (
        await db.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalars().first()
    if convo is None:
        raise HTTPException(status_code=404, detail="Not found")
    is_owner = convo.user_id == user.id
    is_admin = user.role == UserRole.admin
    if not (is_owner or is_admin):
        raise HTTPException(status_code=404, detail="Not found")

    # Delete child messages explicitly. The model relationship may not
    # have ON DELETE CASCADE wired in, and a stale orphan ChatMessage
    # is worse than a slightly chattier DELETE — those rows hold the
    # entire transcript we're trying to scrub.
    await db.execute(
        delete(ChatMessage).where(
            ChatMessage.conversation_id == conversation_id
        )
    )
    await db.delete(convo)
    await db.flush()

    logger.info(
        "chat.conversation.hard_delete actor=%s actor_role=%s "
        "owner=%s conversation=%s admin_override=%s",
        user.id,
        user.role.value if hasattr(user.role, "value") else user.role,
        convo.user_id,
        conversation_id,
        is_admin and not is_owner,
    )
    return None


@router.post(
    "/conversations/{conversation_id}/unarchive",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def unarchive_conversation(
    conversation_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Inverso de ``archive_conversation``: clear ``archived_at`` so the
    conversation reappears in the default panel listing.

    Idempotente: desarchivar una conversación que no estaba archivada
    no rompe nada. Solo el dueño puede desarchivar.
    """
    convo = (
        await db.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalars().first()
    if convo is None or convo.user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")
    if convo.archived_at is not None:
        convo.archived_at = None
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
@limiter.limit(CHAT_STREAM_LIMIT)
async def legacy_chat(
    request: Request,
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
