"""Wishlist tool: ``add_to_wishlist`` reuses the existing
``WantToTryDish`` table. Idempotent — duplicates are absorbed by the
composite primary key.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import WantToTryDish
from app.services.chat.agent_loop import ToolSpec


ADD_TO_WISHLIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dish_id": {"type": "string", "format": "uuid"},
    },
    "required": ["dish_id"],
    "additionalProperties": False,
}


def make_add_to_wishlist_tool(
    db: AsyncSession, *, user_id: uuid.UUID | None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {
                "error": (
                    "User not authenticated. Ask them to log in to save "
                    "dishes."
                )
            }

        stmt = (
            insert(WantToTryDish)
            .values(user_id=user_id, dish_id=args["dish_id"])
            .on_conflict_do_nothing(
                index_elements=["user_id", "dish_id"],
            )
        )
        await db.execute(stmt)
        return {"saved": True, "dish_id": args["dish_id"]}

    return ToolSpec(
        name="add_to_wishlist",
        description=(
            "Save a dish to the user's 'want to try' list. Requires login. "
            "Idempotent."
        ),
        input_schema=ADD_TO_WISHLIST_SCHEMA,
        handler=handler,
    )
