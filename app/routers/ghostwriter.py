"""Ghostwriter endpoint — Phase 2 of the agentic chatbot.

``POST /api/dish-reviews/assist`` accepts an optional photo (URL *or*
multipart upload) plus optional context (``dish_id``, ``draft_text``)
and returns Gemini-generated suggestions:

- Tags the user can pin to the review.
- Visible ingredients we detected.
- A plating style label.
- A 1-2 sentence editorial blurb.
- 0-3 suggested pros and 0-2 suggested cons.

The endpoint is *advisory*: nothing gets written to the DB. The user
keeps full control of what lands in their review.

Authentication: required. We don't want anonymous bots burning Gemini
credits. Rate limiting falls under the same SlowAPI scope as other
review endpoints (handled at app level).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.dish import Dish
from app.models.user import User
from app.services.vision_service import analyze_dish_photo


router = APIRouter(prefix="/api/dish-reviews", tags=["reviews", "ghostwriter"])


# ──────────────────────────────────────────────────────────────────────────
#   Schemas
# ──────────────────────────────────────────────────────────────────────────


class AssistJsonRequest(BaseModel):
    """JSON body for callers that already have the photo on a public URL."""

    dish_id: uuid.UUID | None = None
    photo_url: str | None = Field(default=None, max_length=2000)
    draft_text: str | None = Field(default=None, max_length=4000)


class AssistResponse(BaseModel):
    tags: list[str]
    visible_ingredients: list[str]
    plating_style: str | None
    editorial_blurb: str | None
    suggested_pros: list[str]
    suggested_cons: list[str]
    # When the user passed a draft, we echo back the tags that look like
    # a fit *and* aren't already in the draft. Lets the FE highlight
    # which chips are net-new.
    new_tags: list[str]


# ──────────────────────────────────────────────────────────────────────────
#   Helpers
# ──────────────────────────────────────────────────────────────────────────


async def _resolve_dish_hint(
    db: AsyncSession, dish_id: uuid.UUID | None
) -> str | None:
    if dish_id is None:
        return None
    row = (
        await db.execute(select(Dish.name).where(Dish.id == dish_id))
    ).scalars().first()
    return row


def _filter_new_tags(tags: list[str], draft_text: str | None) -> list[str]:
    if not draft_text:
        return list(tags)
    needle = draft_text.lower()
    return [t for t in tags if t.lower() not in needle]


def _build_response(raw: dict[str, Any], draft_text: str | None) -> AssistResponse:
    return AssistResponse(
        tags=raw["tags"],
        visible_ingredients=raw["visible_ingredients"],
        plating_style=raw["plating_style"],
        editorial_blurb=raw["editorial_blurb"],
        suggested_pros=raw["suggested_pros"],
        suggested_cons=raw["suggested_cons"],
        new_tags=_filter_new_tags(raw["tags"], draft_text),
    )


# ──────────────────────────────────────────────────────────────────────────
#   Endpoints
# ──────────────────────────────────────────────────────────────────────────


@router.post("/assist", response_model=AssistResponse)
async def assist_with_url(
    body: AssistJsonRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> AssistResponse:
    """JSON variant: caller provides ``photo_url``."""
    if not body.photo_url and not body.dish_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a photo_url, a dish_id, or both.",
        )

    dish_hint = await _resolve_dish_hint(db, body.dish_id)
    raw = await analyze_dish_photo(
        photo_url=body.photo_url,
        dish_hint=dish_hint,
    )
    return _build_response(raw, body.draft_text)


@router.post("/assist/upload", response_model=AssistResponse)
async def assist_with_upload(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    photo: Annotated[UploadFile, File(...)],
    dish_id: Annotated[uuid.UUID | None, Form()] = None,
    draft_text: Annotated[str | None, Form(max_length=4000)] = None,
) -> AssistResponse:
    """Multipart variant: caller uploads the photo bytes directly. Avoids
    needing the photo on a public URL before the review is even saved."""
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty photo upload.",
        )
    if len(photo_bytes) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Photo must be 8 MB or smaller.",
        )

    dish_hint = await _resolve_dish_hint(db, dish_id)
    raw = await analyze_dish_photo(
        photo_bytes=photo_bytes,
        photo_mime=photo.content_type,
        dish_hint=dish_hint,
    )
    return _build_response(raw, draft_text)
