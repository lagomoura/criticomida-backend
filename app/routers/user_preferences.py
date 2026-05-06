"""User preferences endpoints (B2C).

Two surfaces, both gated by an authenticated comensal:

- ``GET|PUT /api/users/me/chat-preferences`` — language + response
  style for the Sommelier chat. Mirror of the Business
  ``/api/restaurants/{slug}/owner/chat-preferences`` pair, minus
  the restaurant scope.

- ``GET|PUT /api/users/me/taste-profile`` — read the comensal's
  full ``UserTasteProfile`` and update the two user-declared
  fields (``allergies`` + ``preferred_hours``). The inferred fields
  (``dominant_pillar``, ``top_neighborhoods``, etc.) are read-only
  here — the aggregator owns them.

The ``/me/preferencias`` settings page in the FE consumes both
endpoints so the comensal can see their full profile + edit what's
editable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.chat import UserTasteProfile
from app.models.user import User
from app.services.user_chat_preferences_service import (
    get_user_chat_preferences,
    replace_user_chat_preference,
)


router = APIRouter(tags=["user-preferences"])


# ──────────────────────────────────────────────────────────────────────────
#   Schemas
# ──────────────────────────────────────────────────────────────────────────


_LANGUAGE_LITERAL = Literal["es", "en", "pt"]
_STYLE_LITERAL = Literal["editorial", "concise", "warm"]


class UserChatPreferenceRead(BaseModel):
    """Serialised view of ``UserChatPreference`` for the FE."""

    language_preference: _LANGUAGE_LITERAL | None = None
    response_style: _STYLE_LITERAL | None = None


class UserChatPreferenceUpdate(BaseModel):
    """Form-shaped payload: every field can be ``None`` to clear."""

    language_preference: _LANGUAGE_LITERAL | None = None
    response_style: _STYLE_LITERAL | None = None


class TasteProfileRead(BaseModel):
    """Read-only view of the full taste profile.

    The form on the FE only edits ``allergies`` + ``preferred_hours``;
    the inferred fields (``dominant_pillar``, ``top_neighborhoods``,
    ``top_categories``, ``favorite_tags``, ``avg_price_band``) are
    surfaced here too so the comensal can see what we know about
    them, plus a hint that those update with their reviews.
    """

    dominant_pillar: str | None = None
    top_neighborhoods: list[str] = Field(default_factory=list)
    top_categories: list[str] = Field(default_factory=list)
    favorite_tags: list[str] = Field(default_factory=list)
    avg_price_band: str | None = None
    preferred_hours: list[int] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class TasteProfileUpdate(BaseModel):
    """Only the user-declared fields are writable from the FE."""

    allergies: list[str] = Field(default_factory=list, max_length=20)
    preferred_hours: list[int] = Field(default_factory=list, max_length=24)

    @field_validator("allergies", mode="before")
    @classmethod
    def _strip_allergies(cls, value):
        if isinstance(value, list):
            return [str(a).strip()[:60] for a in value if str(a).strip()]
        return value

    @field_validator("preferred_hours", mode="before")
    @classmethod
    def _validate_hours(cls, value):
        if isinstance(value, list):
            return sorted(
                {int(h) for h in value if isinstance(h, int) and 0 <= h <= 23}
            )
        return value


# ──────────────────────────────────────────────────────────────────────────
#   Chat preferences
# ──────────────────────────────────────────────────────────────────────────


def _serialize_chat_pref(pref) -> UserChatPreferenceRead:
    if pref is None:
        return UserChatPreferenceRead()
    return UserChatPreferenceRead(
        language_preference=pref.language_preference,
        response_style=pref.response_style,
    )


@router.get(
    "/api/users/me/chat-preferences",
    response_model=UserChatPreferenceRead,
)
async def get_my_chat_preferences(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserChatPreferenceRead:
    """Sin fila → todos los campos en ``None`` (defaults del prompt)."""
    pref = await get_user_chat_preferences(db, user_id=current_user.id)
    return _serialize_chat_pref(pref)


@router.put(
    "/api/users/me/chat-preferences",
    response_model=UserChatPreferenceRead,
)
async def update_my_chat_preferences(
    payload: UserChatPreferenceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserChatPreferenceRead:
    """Reemplaza el estado completo. ``None`` limpia la preferencia."""
    pref = await replace_user_chat_preference(
        db,
        user_id=current_user.id,
        language_preference=payload.language_preference,
        response_style=payload.response_style,
    )
    return _serialize_chat_pref(pref)


# ──────────────────────────────────────────────────────────────────────────
#   Taste profile
# ──────────────────────────────────────────────────────────────────────────


def _serialize_taste_profile(prof: UserTasteProfile | None) -> TasteProfileRead:
    if prof is None:
        return TasteProfileRead()
    return TasteProfileRead(
        dominant_pillar=(
            prof.dominant_pillar.value
            if prof.dominant_pillar is not None
            else None
        ),
        top_neighborhoods=list(prof.top_neighborhoods or []),
        top_categories=list(prof.top_categories or []),
        favorite_tags=list(prof.favorite_tags or []),
        avg_price_band=(
            prof.avg_price_band.value
            if prof.avg_price_band is not None
            else None
        ),
        preferred_hours=list(prof.preferred_hours or []),
        allergies=list(prof.allergies or []),
        updated_at=prof.updated_at,
    )


@router.get(
    "/api/users/me/taste-profile",
    response_model=TasteProfileRead,
)
async def get_my_taste_profile(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TasteProfileRead:
    """Lectura del profile completo. Inferidos + declarados, todo en
    el mismo payload — la FE muestra unos como read-only."""
    stmt = select(UserTasteProfile).where(
        UserTasteProfile.user_id == current_user.id
    )
    prof = (await db.execute(stmt)).scalars().first()
    return _serialize_taste_profile(prof)


@router.put(
    "/api/users/me/taste-profile",
    response_model=TasteProfileRead,
)
async def update_my_taste_profile(
    payload: TasteProfileUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> TasteProfileRead:
    """Solo escribe los dos campos editables (``allergies`` y
    ``preferred_hours``). Los inferidos los recalcula el aggregator
    al crear/editar reseñas, no el form."""
    stmt = select(UserTasteProfile).where(
        UserTasteProfile.user_id == current_user.id
    )
    prof = (await db.execute(stmt)).scalars().first()
    if prof is None:
        prof = UserTasteProfile(user_id=current_user.id)
        db.add(prof)
    prof.allergies = list(payload.allergies)
    prof.preferred_hours = list(payload.preferred_hours)
    prof.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return _serialize_taste_profile(prof)
