"""Read/write helpers for the per-owner chat preferences.

The preferences live in ``owner_chat_preferences`` (one row per
``(user_id, restaurant_id)`` pair). The chat service reads them on
session start to personalise the system prompt; the
``update_owner_preferences`` tool writes them when the owner gives
explicit instructions during a conversation ("respondé en portugués",
"siempre mostrame el rating con delta", "tono más formal").

Sin fila → defaults sensatos (tono profesional, idioma del input,
sin KPIs prioritarios). El primer write hace upsert.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.owner_preferences import OwnerChatPreference


async def get_chat_preferences(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    restaurant_id: uuid.UUID,
) -> OwnerChatPreference | None:
    """Return the row for the (owner, restaurant) pair, or ``None`` if
    the owner has never customised anything yet."""
    stmt = select(OwnerChatPreference).where(
        OwnerChatPreference.user_id == user_id,
        OwnerChatPreference.restaurant_id == restaurant_id,
    )
    return (await db.execute(stmt)).scalars().first()


async def replace_chat_preference(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    restaurant_id: uuid.UUID,
    tone_preference: str | None,
    kpi_focus: list[str] | None,
    language_preference: str | None,
) -> OwnerChatPreference:
    """Replace the full state of the (owner, restaurant) preferences.

    Unlike ``upsert_chat_preference`` (where ``None`` means "don't
    touch"), this helper writes **all three fields** every time. It's
    the right primitive for a form-style settings panel: the user
    submits the complete state and we mirror it.

    ``None`` here is "no preference" (clears the column to NULL).
    """
    pref = await get_chat_preferences(
        db, user_id=user_id, restaurant_id=restaurant_id
    )
    if pref is None:
        pref = OwnerChatPreference(
            user_id=user_id,
            restaurant_id=restaurant_id,
            tone_preference=tone_preference,
            kpi_focus=kpi_focus,
            language_preference=language_preference,
        )
        db.add(pref)
    else:
        pref.tone_preference = tone_preference
        pref.kpi_focus = kpi_focus
        pref.language_preference = language_preference
    await db.flush()
    return pref


async def upsert_chat_preference(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    restaurant_id: uuid.UUID,
    tone_preference: str | None = None,
    kpi_focus: list[str] | None = None,
    language_preference: str | None = None,
) -> OwnerChatPreference:
    """Insert-or-update preserving fields the caller didn't pass.

    Each field defaults to ``None`` here meaning "don't touch". To
    explicitly clear a field (e.g. owner says "remové la preferencia
    de idioma"), pass the literal sentinel ``""`` and translate it to
    ``NULL`` at the SQL layer — handled by the tool wrapper, not
    here, so this stays a clean primitive.
    """
    values: dict[str, Any] = {
        "user_id": user_id,
        "restaurant_id": restaurant_id,
    }
    update_set: dict[str, Any] = {}
    if tone_preference is not None:
        values["tone_preference"] = tone_preference
        update_set["tone_preference"] = tone_preference
    if kpi_focus is not None:
        values["kpi_focus"] = kpi_focus
        update_set["kpi_focus"] = kpi_focus
    if language_preference is not None:
        values["language_preference"] = language_preference
        update_set["language_preference"] = language_preference

    stmt = (
        pg_insert(OwnerChatPreference)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["user_id", "restaurant_id"],
            set_=update_set or {"updated_at": values.get("updated_at")},
        )
        .returning(OwnerChatPreference)
    )
    # ``returning`` with ORM entity needs execution_options to hydrate
    # the SQLAlchemy mapped object correctly.
    result = await db.execute(
        stmt, execution_options={"populate_existing": True}
    )
    await db.flush()
    row = result.scalars().first()
    if row is None:
        # Fallback path — re-fetch (shouldn't happen with RETURNING).
        row = await get_chat_preferences(
            db, user_id=user_id, restaurant_id=restaurant_id
        )
    assert row is not None  # for type-checkers
    return row


def render_preferences_block(prefs: OwnerChatPreference | None) -> str | None:
    """Render the preferences as a markdown block to inject into the
    system prompt. Returns ``None`` when there's nothing to inject —
    the caller should skip pasting empty sections."""
    if prefs is None:
        return None
    lines: list[str] = []
    if prefs.tone_preference:
        lines.append(f"- Tono preferido: {prefs.tone_preference}.")
    if prefs.language_preference:
        lines.append(
            f"- Idioma de respuesta preferido: {prefs.language_preference} "
            "(usalo aún si el owner pregunta en otro idioma; el draft de "
            "respuesta a una reseña sigue la regla original — idioma de la "
            "reseña, no del owner)."
        )
    if prefs.kpi_focus:
        lines.append(
            "- KPIs prioritarios para el saludo y los resúmenes: "
            + ", ".join(prefs.kpi_focus)
            + "."
        )
    if not lines:
        return None
    return "# Preferencias del owner (chat)\n" + "\n".join(lines)
