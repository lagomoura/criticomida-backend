"""Resolve the "where was the diner when they opened the chat?" hint.

A — Context Injection. The Sommelier drawer optionally receives a
client-side hint about the page the diner was looking at when they
tapped the floating button (a restaurant detail page, a dish detail
page). This module resolves that hint to a short human-readable
block the agent loop prefixes to the first user message so the model
can ground its first response without us having to type "I was
looking at Sagardi" by hand.

Design notes:

- The block is a **hint**, never a constraint. The agent keeps full
  tool access; if the diner pivots, the agent follows.
- We only resolve on the FIRST user turn (history empty) — once the
  conversation has any turns, the topic has been established and
  re-injecting the context would just bloat tokens.
- We prepend to the **user message**, not the system prompt. The
  system + tools prefix is cached server-side (sha256 of model +
  system + tools, see ``agent_loop._ensure_cached_content``); putting
  per-(user, restaurant)-page text in the system would shred cache
  cardinality and erase the 25%-cost reduction the cache is for.
- Slug / dish_id pointing at deleted rows return ``None`` silently.
  The diner's URL could have been stale (they had the tab open while
  someone moderated the restaurant). Better to lose the hint than to
  drop a misleading block into the prompt.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import Dish
from app.models.restaurant import Restaurant


logger = logging.getLogger(__name__)


async def build_context_hint(
    db: AsyncSession,
    *,
    restaurant_slug: str | None = None,
    restaurant_id: uuid.UUID | None = None,
    dish_id: uuid.UUID | None = None,
) -> str | None:
    """Resolve the FE-provided context to a one-line prefix block.

    All fields default to ``None``. Priority when more than one is
    populated: ``dish_id`` (most specific — already pins a
    restaurant too) > ``restaurant_id`` > ``restaurant_slug``.

    Both restaurant identifiers exist because the FE route for a
    restaurant detail page accepts either ``/restaurants/{slug}`` or
    ``/restaurants/{uuid}`` — the launcher sends whichever one is in
    the path so the backend can resolve without a slug↔id round-trip.

    Returns ``None`` when:
    - All fields are empty.
    - The referenced entity no longer exists (stale URL).
    """
    if dish_id is not None:
        row = (
            await db.execute(
                select(
                    Dish.name,
                    Dish.restaurant_id,
                    Restaurant.name.label("restaurant_name"),
                    Restaurant.slug.label("restaurant_slug"),
                )
                .join(Restaurant, Restaurant.id == Dish.restaurant_id)
                .where(Dish.id == dish_id)
            )
        ).first()
        if row is None:
            logger.debug(
                "context hint skipped: dish %s not found", dish_id
            )
            return None
        # Include the canonical identifiers in the hint so the LLM never
        # has to invent / re-derive them. Without this, agents reach for
        # ``search_dishes(restaurant_id=<hallucinated uuid>)`` to scope
        # discovery to the current restaurant and burn iterations until a
        # later tool reveals the real uuid — observed in prod logs.
        return (
            f'[contexto: el comensal está mirando la página del plato '
            f'"{row.name}" (dish_id={dish_id}) en "{row.restaurant_name}" '
            f"(restaurant_id={row.restaurant_id}, "
            f"restaurant_slug={row.restaurant_slug}). Usá estos "
            "identificadores cuando llames tools que los necesiten "
            "(ej. search_dishes(restaurant_id=...) para acotar a este "
            "lugar, list_restaurant_reviews(restaurant_id=...) para "
            "opiniones). Es pista de orientación, no filtro obligatorio.]"
        )

    if restaurant_id is not None:
        row = (
            await db.execute(
                select(Restaurant.name, Restaurant.slug).where(
                    Restaurant.id == restaurant_id
                )
            )
        ).first()
        if row is None:
            logger.debug(
                "context hint skipped: restaurant id %s not found",
                restaurant_id,
            )
            return None
        return (
            f'[contexto: el comensal está mirando la página del '
            f'restaurante "{row.name}" (restaurant_id={restaurant_id}, '
            f"restaurant_slug={row.slug}). Usá estos identificadores "
            "cuando llames tools que los necesiten (ej. "
            "search_dishes(restaurant_id=...), "
            "list_restaurant_reviews(restaurant_id=...)). Es pista de "
            "orientación, no filtro obligatorio.]"
        )

    if restaurant_slug:
        row = (
            await db.execute(
                select(Restaurant.id, Restaurant.name).where(
                    Restaurant.slug == restaurant_slug
                )
            )
        ).first()
        if row is None:
            logger.debug(
                "context hint skipped: restaurant slug %s not found",
                restaurant_slug,
            )
            return None
        return (
            f'[contexto: el comensal está mirando la página del '
            f'restaurante "{row.name}" (restaurant_id={row.id}, '
            f"restaurant_slug={restaurant_slug}). Usá estos "
            "identificadores cuando llames tools que los necesiten "
            "(ej. search_dishes(restaurant_id=...), "
            "list_restaurant_reviews(restaurant_id=...)). Es pista de "
            "orientación, no filtro obligatorio.]"
        )

    return None
