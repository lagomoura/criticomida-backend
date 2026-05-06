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
from app.services.chat.tools._allergy_filter import (
    allergen_canonical_key,
)


UPDATE_TASTE_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "allergies": {
            "type": "array",
            "items": {"type": "string", "minLength": 2, "maxLength": 60},
            "description": (
                "Free-form allergy/restriction declarations as the user "
                "stated them, **as full words**, e.g. ['gluten', 'maní', "
                "'nueces']. NEVER pass single characters or split a word "
                "across array items: ['m', 'a', 'n', 'í'] is wrong; "
                "['maní'] is right. Only call this when the user "
                "explicitly mentions an allergy or dietary restriction "
                "in plain language."
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
        # Defence: Gemini Flash Lite occasionally emits a tool call
        # with ``arguments: "{}"`` after the user declared an allergy
        # — the model "thinks" it remembered the value but didn't
        # serialise it. Without this guard the handler short-circuits
        # without touching the DB and reports ``saved: True``, leading
        # the agent to confirm verbally a save that never happened.
        # Fail loudly so the agent re-emits the call with the actual
        # payload on the next iteration.
        allergies_in = args.get("allergies")
        hours_in = args.get("preferred_hours")
        if allergies_in is None and hours_in is None:
            return {
                "saved": False,
                "error": "missing_input",
                "message": (
                    "Llamaste update_taste_profile sin pasar "
                    "``allergies`` ni ``preferred_hours``. Re-emití "
                    "la llamada incluyendo el array que el comensal "
                    "declaró (ej: ``allergies=['maní']``). NO le digas "
                    "al comensal que guardaste algo — todavía no se "
                    "guardó."
                ),
            }

        # Defence vs Flash Lite: occasionally the model serialises
        # a single allergy word as an array of CHARACTERS — e.g.
        # passing ``allergies=['m', 'a', 'n', 'í']`` when the user
        # said "soy alérgico al maní". Each item is one char and
        # the merge happily writes them all to DB, polluting the
        # profile permanently. Reject any single-character item;
        # the agent has to re-emit the call with the full word.
        if isinstance(allergies_in, list):
            single_chars = [
                a for a in allergies_in
                if isinstance(a, str) and len(a.strip()) < 2
            ]
            if single_chars:
                return {
                    "saved": False,
                    "error": "invalid_format",
                    "message": (
                        f"Pasaste allergies con items de un solo "
                        f"carácter: {single_chars!r}. Eso suele ser "
                        "el modelo serializando una palabra como "
                        "array de caracteres ('maní' → ['m','a','n','í']). "
                        "Re-emití la llamada con la PALABRA COMPLETA "
                        "como un único string en el array — ej: "
                        "``allergies=['maní']``, no ``allergies=['m','a','n','í']``. "
                        "NO le digas al comensal que guardaste algo "
                        "— todavía no se guardó."
                    ),
                }

        if user_id is None:
            # Anonymous comensal: nothing was persisted. The error
            # message is for YOU (the agent) — bake the instruction
            # into it so the model doesn't accidentally close its
            # turn with "anoté tus preferencias" anyway, which we
            # saw in production. The contract: acknowledge the
            # declaration in this conversation only, invite login
            # for cross-session persistence, and do NOT claim the
            # data was saved.
            return {
                "saved": False,
                "error": "not_authenticated",
                "message": (
                    "El comensal no está logueado: el profile NO se "
                    "guardó. PROHIBIDO responder con 'anoté tus "
                    "preferencias', 'lo guardé', 'lo tomo en cuenta "
                    "para futuras conversaciones' o equivalentes — "
                    "esa frase miente. Respondé que vas a respetar "
                    "lo declarado SOLO durante esta conversación, y "
                    "que para que lo recordemos en el futuro tiene "
                    "que iniciar sesión y volver a declararlo. "
                    "Después seguí ayudándolo en el turno actual "
                    "respetando la restricción que mencionó."
                ),
            }

        stmt = select(UserTasteProfile).where(
            UserTasteProfile.user_id == user_id
        )
        result = await db.execute(stmt)
        profile = result.scalars().first()
        if profile is None:
            profile = UserTasteProfile(user_id=user_id)
            db.add(profile)

        if (allergies := args.get("allergies")) is not None:
            # Merge case-insensitive against the existing list. The
            # chat is incremental: when the comensal says "soy
            # alérgico al maní" today and "y a las nueces" tomorrow
            # we want both stored, not the last one. Replacing was a
            # production bug — the model usually only sends the
            # *new* allergy in its tool call, not the full set, so
            # ``profile.allergies = cleaned`` clobbered earlier
            # declarations.
            #
            # To REMOVE an allergy the comensal goes to
            # ``/me/preferencias`` (form does PUT replace) — there's
            # no current path through the chat for explicit
            # removal, deliberately: a tool that lets the LLM drop
            # allergies on inferred intent is a safety footgun.
            # Dedup by canonical key, not raw lowercase. Without
            # this the comensal could end up with ["nuez", "nueces"]
            # or ["maní", "peanut"] in their profile — same allergen,
            # different surface forms. ``allergen_canonical_key``
            # collapses synonym groups (nuez/nueces/walnut → "nuez")
            # so a second declaration of the same restriction is a
            # no-op. We KEEP the original first form the user typed
            # — don't rewrite "nueces" to "nuez" silently; the
            # comensal sees their own word in /me/preferencias.
            incoming = [a.strip() for a in allergies if a and a.strip()]
            existing = list(profile.allergies or [])
            existing_keys = {
                allergen_canonical_key(a)
                for a in existing
                if allergen_canonical_key(a)
            }
            for item in incoming:
                key = allergen_canonical_key(item)
                if not key or key in existing_keys:
                    continue
                existing.append(item)
                existing_keys.add(key)
            profile.allergies = existing[:20]

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
            "restriction or schedule preference. Never guess. "
            "**``allergies`` is additive**: pass only the NEW allergy "
            "the user just declared — the handler merges it with the "
            "existing list (case-insensitive dedupe). Don't re-send "
            "previously declared allergies; that's wasted tokens. "
            "To REMOVE an allergy the user goes through the "
            "``/me/preferencias`` form (this tool intentionally has "
            "no remove path)."
        ),
        input_schema=UPDATE_TASTE_PROFILE_SCHEMA,
        handler=handler,
    )
