"""Schema liviano para el autocomplete de menciones.

No reusa ``UserSearchResult`` porque el dropdown necesita menos campos y el
endpoint debe ser barato (~10 rows máx)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class UserMentionSuggestion(BaseModel):
    id: uuid.UUID
    display_name: str
    handle: str
    avatar_url: str | None = None
