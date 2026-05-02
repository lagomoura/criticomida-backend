"""``open_in_map`` is a UI-only tool: it returns a payload the frontend
uses to navigate the user to the discovery map preloaded with a bbox or a
list of dishes. No DB writes — the LLM emits this when the user wants to
*see* what was suggested on the map.
"""

from __future__ import annotations

from typing import Any

from app.services.chat.agent_loop import ToolSpec


OPEN_IN_MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "bbox": {
            "type": "object",
            "properties": {
                "south": {"type": "number"},
                "west": {"type": "number"},
                "north": {"type": "number"},
                "east": {"type": "number"},
            },
            "required": ["south", "west", "north", "east"],
        },
        "center": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lng": {"type": "number"},
                "zoom": {"type": "integer", "minimum": 8, "maximum": 18},
            },
            "required": ["lat", "lng"],
        },
        "dish_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "Dishes to highlight as pins on the map.",
        },
    },
    "additionalProperties": False,
}


def make_open_in_map_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # Pure passthrough: validate one-of and let the FE handle navigation.
        if not any(k in args for k in ("bbox", "center", "dish_ids")):
            return {
                "error": "Provide at least one of: bbox, center, dish_ids."
            }
        return {
            "action": "open_in_map",
            "bbox": args.get("bbox"),
            "center": args.get("center"),
            "dish_ids": args.get("dish_ids", []),
        }

    return ToolSpec(
        name="open_in_map",
        description=(
            "Open the CritiComida discovery map. Use a bbox for an area "
            "('Palermo'), a center for a point of interest, or a list of "
            "dish_ids to drop pins on specific dishes you just suggested."
        ),
        input_schema=OPEN_IN_MAP_SCHEMA,
        handler=handler,
        emits_card=True,
    )
