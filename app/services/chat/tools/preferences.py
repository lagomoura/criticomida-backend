"""``update_user_chat_preferences`` — Sommelier persistent prefs.

Mirror of ``update_owner_preferences`` for the B2C side. Persists
language + response_style for the comensal so future sessions can
inherit the choice. The chat service injects the saved values at the
top of the system prompt at the start of every turn.

Two important contract choices:

- **Persistence is for FUTURE sessions.** A change made mid-session
  applies on the next conversation; the current turn keeps the state
  it started with. This matches the Business contract and prevents
  weird mid-turn personality flips.

- **Empty string ``""`` clears a column.** ``None`` means "don't
  touch". The schema layer (Pydantic) emits ``None`` when the field
  was omitted and an empty enum value is rejected before the handler
  runs — so to actually clear language/style, the caller has to send
  the literal empty string. We translate that to ``None`` at the SQL
  layer here.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._schemas import (
    UpdateUserChatPreferencesInput,
    pydantic_to_anthropic_schema,
)
from app.services.user_chat_preferences_service import (
    upsert_user_chat_preference,
)


def make_update_user_chat_preferences_tool(
    db: AsyncSession, *, user_id: uuid.UUID | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            # Anonymous comensal: persistence makes no sense without
            # a stable identity. Same defensive guidance pattern the
            # update_taste_profile tool uses (regla 5 prompt).
            return {
                "saved": False,
                "error": "not_authenticated",
                "message": (
                    "El comensal no está logueado: el profile no se "
                    "guardó. PROHIBIDO responder con 'anoté tus "
                    "preferencias', 'lo guardé', 'lo tomo en cuenta "
                    "para futuras conversaciones' o equivalentes — "
                    "esa frase miente. Respondé que vas a respetar "
                    "lo declarado SOLO durante esta conversación, "
                    "y que para persistirlo el comensal tiene que "
                    "iniciar sesión y volver a pedirlo."
                ),
            }

        try:
            inputs = UpdateUserChatPreferencesInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for update_user_chat_preferences.",
                "details": exc.errors(include_url=False),
            }

        if inputs.language is None and inputs.response_style is None:
            return {
                "error": "missing_input",
                "message": (
                    "Pasame al menos uno de ``language`` o "
                    "``response_style``. Si no estás seguro, no "
                    "llames el tool."
                ),
            }

        pref = await upsert_user_chat_preference(
            db,
            user_id=user_id,
            language_preference=(
                inputs.language.value if inputs.language is not None else None
            ),
            response_style=(
                inputs.response_style.value
                if inputs.response_style is not None
                else None
            ),
        )
        return {
            "saved": True,
            "language_preference": pref.language_preference,
            "response_style": pref.response_style,
            "applies_from": "next_session",
        }

    return ToolSpec(
        name="update_user_chat_preferences",
        description=(
            "Persist Sommelier preferences for the comensal so future "
            "sessions inherit the choice. Pass ``language`` (es/en/pt) "
            "and/or ``response_style`` (editorial/concise/warm). Only "
            "call this when the comensal asks for something **explicitly "
            "persistent** — 'siempre respondé en inglés', 'de ahora en "
            "más hablame corto', 'a partir de hoy más conversacional'. "
            "For one-off tweaks within a single turn, do NOT persist; "
            "just adjust the response. Persistence applies from the "
            "**next session**: the current turn keeps the state it "
            "started with. Confirm briefly and continue. Anonymous "
            "comensales get ``saved: false`` — never claim you saved "
            "anything in that case."
        ),
        input_schema=pydantic_to_anthropic_schema(
            UpdateUserChatPreferencesInput
        ),
        handler=handler,
    )
