"""Read/write helpers for the per-comensal Sommelier preferences.

Mirror of ``owner_chat_preferences_service`` for the B2C side. The
preferences live in ``user_chat_preferences`` (one row per
``user_id``). The chat service reads them on session start to
personalise the system prompt; the ``update_user_chat_preferences``
tool writes them when the comensal gives explicit instructions
during a conversation ("siempre respondé en inglés", "hablame
corto", "andá al grano").

Sin fila → defaults sensatos (idioma del input, voz editorial del
prompt). El primer write hace upsert.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_preferences import UserChatPreference


_RESPONSE_STYLE_LABEL_ES: dict[str, str] = {
    "editorial": "editorial (2-3 frases enmarcando)",
    "concise": "conciso (una frase + cards, sin rodeos)",
    "warm": "cálido y conversacional",
}


async def get_user_chat_preferences(
    db: AsyncSession, *, user_id: uuid.UUID
) -> UserChatPreference | None:
    """Return the row for the comensal, or ``None`` if they haven't
    customised anything yet (the prompt's defaults apply)."""
    stmt = select(UserChatPreference).where(
        UserChatPreference.user_id == user_id
    )
    return (await db.execute(stmt)).scalars().first()


async def replace_user_chat_preference(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    language_preference: str | None,
    response_style: str | None,
) -> UserChatPreference:
    """Replace the full state of the comensal's preferences.

    Form-shaped: every call writes BOTH fields. ``None`` here means
    "no preference" (column → NULL). For the partial-update / "don't
    touch unless mentioned" semantics the chat tool needs, see
    ``upsert_user_chat_preference``.
    """
    pref = await get_user_chat_preferences(db, user_id=user_id)
    if pref is None:
        pref = UserChatPreference(
            user_id=user_id,
            language_preference=language_preference,
            response_style=response_style,
        )
        db.add(pref)
    else:
        pref.language_preference = language_preference
        pref.response_style = response_style
    await db.flush()
    return pref


async def upsert_user_chat_preference(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    language_preference: str | None = None,
    response_style: str | None = None,
) -> UserChatPreference:
    """Insert-or-update preserving fields the caller didn't pass.

    Each field defaults to ``None`` here meaning **"don't touch"**.
    Pass the literal empty string ``""`` from the tool wrapper to
    explicitly clear a column.
    """
    values: dict[str, Any] = {"user_id": user_id}
    update_set: dict[str, Any] = {}
    if language_preference is not None:
        values["language_preference"] = language_preference
        update_set["language_preference"] = language_preference
    if response_style is not None:
        values["response_style"] = response_style
        update_set["response_style"] = response_style

    stmt = (
        pg_insert(UserChatPreference)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_=update_set or {"updated_at": values.get("updated_at")},
        )
        .returning(UserChatPreference)
    )
    result = await db.execute(
        stmt, execution_options={"populate_existing": True}
    )
    await db.flush()
    row = result.scalars().first()
    if row is None:
        # Fallback path — re-fetch (shouldn't happen with RETURNING).
        row = await get_user_chat_preferences(db, user_id=user_id)
    assert row is not None  # for type-checkers
    return row


def render_user_preferences_block(
    prefs: UserChatPreference | None,
) -> str | None:
    """Render the comensal preferences as a markdown block to inject
    into the system prompt. Returns ``None`` when there's nothing to
    inject — the caller should skip pasting empty sections."""
    if prefs is None:
        return None
    lines: list[str] = []
    if prefs.language_preference:
        lines.append(
            f"- Idioma de respuesta preferido: {prefs.language_preference} "
            "(usalo aún si el comensal pregunta en otro idioma)."
        )
    if prefs.response_style:
        label = _RESPONSE_STYLE_LABEL_ES.get(
            prefs.response_style, prefs.response_style
        )
        lines.append(f"- Estilo de respuesta preferido: {label}.")
    if not lines:
        return None
    return "# Preferencias del comensal (chat)\n" + "\n".join(lines)
