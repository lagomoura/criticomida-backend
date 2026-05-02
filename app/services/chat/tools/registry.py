"""Factory that wires ``ToolRegistry`` for a given agent + user context.

Each agent gets a different toolbelt:

- ``sommelier`` (B2C): search, dish detail, wishlist, map, taste updates.
- ``ghostwriter`` (Phase 2): tag/visual analysis tools (added later).
- ``business`` (Phase 3): analytics tools, scoped to a single restaurant.

Building the registry per request lets us inject the SQLAlchemy session
and the authenticated user into each tool's closure, keeping the tools
themselves free of FastAPI globals.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatAgent
from app.services.chat.agent_loop import ToolRegistry
from app.services.chat.tools.business import (
    make_analyze_dish_pillar_drop_tool,
    make_benchmark_dish_tool,
    make_list_pending_reviews_tool,
)
from app.services.chat.tools.map import make_open_in_map_tool
from app.services.chat.tools.reservations import make_request_reservation_tool
from app.services.chat.tools.routes import make_create_dish_route_tool
from app.services.chat.tools.search import (
    make_get_dish_detail_tool,
    make_search_dishes_tool,
)
from app.services.chat.tools.taste import make_update_taste_profile_tool
from app.services.chat.tools.vision import make_suggest_tags_from_photo_tool
from app.services.chat.tools.wishlist import make_add_to_wishlist_tool


EmbedQuery = Callable[[str], Awaitable[list[float]]]


def build_registry(
    *,
    agent: ChatAgent,
    db: AsyncSession,
    user_id: uuid.UUID | None,
    embed_query: EmbedQuery | None,
    restaurant_scope_id: str | None = None,
    conversation_id: uuid.UUID | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    if agent in (ChatAgent.sommelier, ChatAgent.business, ChatAgent.ghostwriter):
        # All three agents may want to look up dishes by name/filters.
        registry.register(
            make_search_dishes_tool(
                db,
                embed_query=embed_query,
                restaurant_scope_id=restaurant_scope_id,
            )
        )
        registry.register(make_get_dish_detail_tool(db))

    if agent == ChatAgent.sommelier:
        registry.register(make_add_to_wishlist_tool(db, user_id=user_id))
        registry.register(make_open_in_map_tool())
        registry.register(make_update_taste_profile_tool(db, user_id=user_id))
        registry.register(
            make_create_dish_route_tool(
                db,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        )
        registry.register(
            make_request_reservation_tool(
                db,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        )

    if agent == ChatAgent.ghostwriter:
        # Editorial assistance: vision tagging + allergy declarations.
        registry.register(make_suggest_tags_from_photo_tool())
        registry.register(make_update_taste_profile_tool(db, user_id=user_id))

    if agent == ChatAgent.business:
        registry.register(
            make_analyze_dish_pillar_drop_tool(
                db, restaurant_scope_id=restaurant_scope_id
            )
        )
        registry.register(
            make_benchmark_dish_tool(
                db, restaurant_scope_id=restaurant_scope_id
            )
        )
        registry.register(
            make_list_pending_reviews_tool(
                db, restaurant_scope_id=restaurant_scope_id
            )
        )

    return registry
