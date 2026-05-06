"""``open_in_map`` is a UI-only tool: it returns a payload the frontend
uses to navigate the user to the discovery map preloaded with a bbox or a
list of dishes. No DB writes — the LLM emits this when the user wants to
*see* what was suggested on the map.

The Pydantic schema enforces that at least one of bbox/center/dish_ids
arrived; lat/lng/zoom ranges are validated server-side; out-of-band
values (like a 25-zoom level) round-trip back to the model as a clean
ValidationError so it can correct on the next iteration.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._schemas import (
    OpenInMapInput,
    pydantic_to_anthropic_schema,
)


def make_open_in_map_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            inputs = OpenInMapInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for open_in_map.",
                "details": exc.errors(include_url=False),
            }

        if (
            inputs.bbox is None
            and inputs.center is None
            and not inputs.dish_ids
        ):
            return {
                "error": "Provide at least one of: bbox, center, dish_ids.",
            }

        return {
            "action": "open_in_map",
            "bbox": (
                inputs.bbox.model_dump() if inputs.bbox is not None else None
            ),
            "center": (
                inputs.center.model_dump(exclude_none=True)
                if inputs.center is not None
                else None
            ),
            "dish_ids": list(inputs.dish_ids or []),
        }

    return ToolSpec(
        name="open_in_map",
        description=(
            "Open the CritiComida discovery map. Use a bbox for an area "
            "('Palermo'), a center for a point of interest, or a list of "
            "dish_ids to drop pins on specific dishes you just suggested. "
            "Pure UI tool — no DB writes — the FE handles the navigation."
        ),
        input_schema=pydantic_to_anthropic_schema(OpenInMapInput),
        handler=handler,
        emits_card=True,
    )
