"""``update_taste_profile`` lets the bot persist things the user
explicitly declared. Only a narrow set of fields is writable here:

- ``allergies`` — never inferable, must come from a direct statement.
- ``preferred_hours`` — also explicit ("solo voy a cenar tarde").

Everything else (dominant_pillar, top_neighborhoods, top_categories,
favorite_tags, avg_price_band) is owned by ``taste_profile_service`` and
recomputed from review history. Letting the LLM overwrite those fields
would invite hallucinated personalization.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import UserTasteProfile
from app.services.chat.agent_loop import ToolSpec


UPDATE_TASTE_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "allergies": {
            "type": "array",
            "items": {"type": "string", "maxLength": 60},
            "description": (
                "Free-form allergy/restriction declarations as the user "
                "stated them, e.g. ['gluten', 'maní']. Only call this when "
                "the user explicitly mentions an allergy or dietary "
                "restriction in plain language."
            ),
        },
        "preferred_hours": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 23},
            "description": (
                "Hours of the day (0-23) the user typically eats out, only "
                "if they explicitly declared it."
            ),
        },
    },
    "additionalProperties": False,
}


def make_update_taste_profile_tool(
    db: AsyncSession, *, user_id: uuid.UUID | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {"error": "User not authenticated."}

        stmt = select(UserTasteProfile).where(
            UserTasteProfile.user_id == user_id
        )
        result = await db.execute(stmt)
        profile = result.scalars().first()
        if profile is None:
            profile = UserTasteProfile(user_id=user_id)
            db.add(profile)

        if (allergies := args.get("allergies")) is not None:
            cleaned = [a.strip() for a in allergies if a and a.strip()][:20]
            profile.allergies = cleaned

        if (hours := args.get("preferred_hours")) is not None:
            profile.preferred_hours = sorted({int(h) for h in hours if 0 <= int(h) <= 23})

        profile.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return {
            "saved": True,
            "allergies": profile.allergies,
            "preferred_hours": profile.preferred_hours,
        }

    return ToolSpec(
        name="update_taste_profile",
        description=(
            "Persist user-declared preferences (allergies, preferred hours). "
            "Only call this when the user has explicitly stated a "
            "restriction or schedule preference. Never guess."
        ),
        input_schema=UPDATE_TASTE_PROFILE_SCHEMA,
        handler=handler,
    )
