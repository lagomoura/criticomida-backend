"""Schemas para los endpoints de block / mute."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class BlockActionResponse(BaseModel):
    """Response para POST/DELETE /api/users/{id_or_handle}/block."""

    blocker_id: uuid.UUID
    blocked_id: uuid.UUID
    blocked: bool


class MuteActionResponse(BaseModel):
    """Response para POST/DELETE /api/users/{id_or_handle}/mute."""

    muter_id: uuid.UUID
    muted_id: uuid.UUID
    muted: bool


class SafetyUserSummary(BaseModel):
    """Resumen mínimo de usuario para listar bloqueados/muteados."""

    id: uuid.UUID
    display_name: str
    handle: str | None = None
    avatar_url: str | None = None
    created_at: datetime  # cuando se creó el block/mute, no el user

    model_config = {"from_attributes": True}


class SafetyUsersPage(BaseModel):
    items: list[SafetyUserSummary]
    next_cursor: str | None = None
