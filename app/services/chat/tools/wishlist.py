"""Wishlist tool: ``add_to_wishlist`` reuses the existing
``WantToTryDish`` table. Idempotent — duplicates are absorbed by the
composite primary key.

Accepts ``dish_id`` or ``dish_name`` so the comensal can save a plato
the same way they talked about it ("guardame el risotto"). The shared
resolver short-circuits ambiguity — disambiguation candidates, no_match
with a search_dishes hint — so the LLM never asks for a UUID.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import WantToTryDish
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._resolution import _resolve_dish_global
from app.services.chat.tools._schemas import (
    AddToWishlistInput,
    pydantic_to_anthropic_schema,
)


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

        try:
            inputs = AddToWishlistInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for add_to_wishlist.",
                "details": exc.errors(include_url=False),
            }

        dish, error = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id=inputs.dish_id,
            dish_name=inputs.dish_name,
            actor="comensal",
        )
        if error is not None:
            return error
        assert dish is not None

        stmt = (
            insert(WantToTryDish)
            .values(user_id=user_id, dish_id=dish.id)
            .on_conflict_do_nothing(
                index_elements=["user_id", "dish_id"],
            )
        )
        await db.execute(stmt)
        return {
            "saved": True,
            "dish_id": str(dish.id),
            "dish_name": dish.name,
        }

    return ToolSpec(
        name="add_to_wishlist",
        description=(
            "Save a dish to the comensal's 'want to try' list. Accepts "
            "``dish_id`` (UUID from search_dishes) or ``dish_name`` "
            "(free text like 'el risotto'). Idempotent — saving the "
            "same dish twice is a no-op. Requires login. NEVER ask the "
            "comensal for a UUID."
        ),
        input_schema=pydantic_to_anthropic_schema(AddToWishlistInput),
        handler=handler,
    )
