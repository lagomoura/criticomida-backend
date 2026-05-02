"""``suggest_tags_from_photo`` is the chat-side counterpart of the
``/api/dish-reviews/assist`` endpoint. The Ghostwriter agent uses it
when the user pastes a photo URL into the conversation: the bot can
return tags + a blurb without making them open the formal review form.
"""

from __future__ import annotations

from typing import Any

from app.services.chat.agent_loop import ToolSpec
from app.services.vision_service import analyze_dish_photo


SUGGEST_TAGS_FROM_PHOTO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "photo_url": {
            "type": "string",
            "description": "Public URL of the dish photo to analyze.",
        },
        "dish_hint": {
            "type": "string",
            "description": (
                "Optional dish name to bias the model — e.g. 'risotto' "
                "helps the bot pick saffron over generic 'rice' tags."
            ),
        },
    },
    "required": ["photo_url"],
    "additionalProperties": False,
}


def make_suggest_tags_from_photo_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = await analyze_dish_photo(
            photo_url=args["photo_url"],
            dish_hint=args.get("dish_hint"),
        )
        return {
            "tags": result["tags"],
            "visible_ingredients": result["visible_ingredients"],
            "plating_style": result["plating_style"],
            "editorial_blurb": result["editorial_blurb"],
            "suggested_pros": result["suggested_pros"],
            "suggested_cons": result["suggested_cons"],
        }

    return ToolSpec(
        name="suggest_tags_from_photo",
        description=(
            "Analyze a dish photo and return suggested tags, ingredients, "
            "plating style, an editorial blurb, and pros/cons. Use this "
            "when the user shares a photo and wants help describing or "
            "tagging the dish."
        ),
        input_schema=SUGGEST_TAGS_FROM_PHOTO_SCHEMA,
        handler=handler,
        # Vision calls are heavier than DB lookups: give them more headroom.
        timeout_seconds=30.0,
        emits_card=True,
    )
