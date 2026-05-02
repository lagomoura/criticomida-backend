"""``create_dish_route`` builds a curated list of dishes from the chat.

Used by the Sommelier when the user says "armame una ruta de 3 platos
ganadores en el centro" or similar. The tool:

1. Validates that all ``dish_ids`` exist (silently skips missing ones).
2. Generates a URL-safe slug from the name + a short random suffix so
   ``/listas/{slug}`` collisions don't happen even with repeated names.
3. Persists ``dish_lists`` + ``dish_list_items`` in one transaction.
4. Returns the slug + a public URL the FE renders as a RouteCard.

Anonymous users can't create lists: the tool surfaces a friendly error
instead of attempting to write under a NULL owner_user_id (the FK
forbids it anyway).
"""

from __future__ import annotations

import re
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.dish import Dish
from app.models.dish_list import DishList, DishListItem
from app.services.chat.agent_loop import ToolSpec


CREATE_DISH_ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 3,
            "maxLength": 160,
            "description": (
                "Title of the route, e.g. 'Ruta de pastas en Belgrano'."
            ),
        },
        "description": {
            "type": "string",
            "maxLength": 600,
            "description": (
                "One-paragraph editorial framing of the route. Optional."
            ),
        },
        "dish_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "minItems": 2,
            "maxItems": 10,
        },
        "is_public": {
            "type": "boolean",
            "default": True,
            "description": (
                "When true (default), the list is reachable at a public "
                "URL. Set to false only if the user explicitly says they "
                "want it private."
            ),
        },
    },
    "required": ["name", "dish_ids"],
    "additionalProperties": False,
}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    base = _SLUG_RE.sub("-", name.lower()).strip("-")
    base = base[:80] or "lista"
    suffix = secrets.token_hex(3)  # 6 hex chars — collision rate is fine
    return f"{base}-{suffix}"


def make_create_dish_route_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if user_id is None:
            return {
                "error": (
                    "User not authenticated. Ask them to log in to save "
                    "a route."
                )
            }

        # Validate dish_ids: keep only those that actually exist.
        ids: list[str] = list(dict.fromkeys(args["dish_ids"]))  # dedupe
        existing = (
            await db.execute(
                select(Dish.id).where(Dish.id.in_(ids))
            )
        ).scalars().all()
        valid = [str(d) for d in existing]
        # Preserve the order the LLM passed (semantic ranking matters).
        ordered = [d for d in ids if d in valid]
        if len(ordered) < 2:
            return {
                "error": (
                    "At least 2 valid dish_ids are required. "
                    f"Received {len(ids)}, valid {len(ordered)}."
                )
            }

        is_public = bool(args.get("is_public", True))
        slug = _slugify(args["name"])

        dish_list = DishList(
            id=uuid.uuid4(),
            owner_user_id=user_id,
            slug=slug,
            name=args["name"],
            description=args.get("description"),
            is_public=is_public,
            source_conversation_id=conversation_id,
        )
        db.add(dish_list)
        await db.flush()

        for idx, dish_id in enumerate(ordered):
            db.add(
                DishListItem(
                    list_id=dish_list.id,
                    dish_id=uuid.UUID(dish_id),
                    position=idx,
                )
            )

        public_url = (
            f"{settings.PUBLIC_APP_URL}/es/listas/{slug}"
            if is_public
            else None
        )
        return {
            "list_id": str(dish_list.id),
            "slug": slug,
            "name": dish_list.name,
            "description": dish_list.description,
            "is_public": is_public,
            "public_url": public_url,
            "dish_count": len(ordered),
            "dish_ids": ordered,
        }

    return ToolSpec(
        name="create_dish_route",
        description=(
            "Create a curated dish route ('ruta') from a sequence of "
            "dish_ids. Use when the user asks to bundle the suggestions "
            "into a shareable list. Default to is_public=true unless "
            "the user explicitly asks for it to stay private."
        ),
        input_schema=CREATE_DISH_ROUTE_SCHEMA,
        handler=handler,
        emits_card=True,
    )
