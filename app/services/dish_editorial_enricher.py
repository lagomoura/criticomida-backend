"""Dish editorial blurb enrichment via LLM.

Generates a 2–3 sentence editorial blurb about each dish in the context of
its host restaurant. Persists the result on `dishes.editorial_blurb` so the
generation runs at most once per dish (refreshable via admin endpoint).

Uses litellm so we stay provider-agnostic; default model is Anthropic's
Haiku 4.5 (fast and cheap, suitable for a one-shot blurb). When no API key is
configured the service degrades silently — same pattern as
`google_places_enricher`.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

import litellm
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.dish import Dish
from app.models.restaurant import Restaurant

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Eres un editor gastronómico para CritiComida. Te dan el nombre de un "
    "plato y el restaurante que lo sirve. Tu tarea: escribir un blurb breve "
    "(2 a 3 oraciones, máximo 60 palabras) en español rioplatense neutro, "
    "que cuente brevemente el origen o tradición del plato y por qué tiene "
    "sentido encontrarlo en ese local específico. Tono editorial, evocativo "
    "pero sobrio. No uses listas ni emojis. No inventes premios ni datos "
    "específicos del local que no estén en el contexto: solo conocimiento "
    "general del plato + el contexto cultural/gastronómico."
)


def _model() -> str:
    return os.getenv(
        "EDITORIAL_MODEL",
        os.getenv("CHAT_MODEL", "anthropic/claude-haiku-4-5-20251001"),
    )


def _api_key() -> str | None:
    return os.getenv("EDITORIAL_API_KEY") or os.getenv("CHAT_API_KEY") or os.getenv(
        "ANTHROPIC_API_KEY"
    )


def _build_user_prompt(dish: Dish, restaurant: Restaurant) -> str:
    parts = [
        f"Plato: {dish.name}",
        f"Restaurante: {restaurant.name}",
        f"Ubicación: {restaurant.location_name}",
    ]
    if restaurant.city:
        parts.append(f"Ciudad: {restaurant.city}")
    if restaurant.cuisine_types:
        parts.append(f"Cocina: {', '.join(restaurant.cuisine_types[:6])}")
    if restaurant.description:
        parts.append(f"Sobre el local: {restaurant.description[:400]}")
    if dish.description:
        parts.append(f"Descripción del plato: {dish.description[:400]}")
    parts.append(
        "Escribí el blurb editorial siguiendo las instrucciones del sistema."
    )
    return "\n".join(parts)


async def _generate_blurb(dish: Dish, restaurant: Restaurant) -> str | None:
    api_key = _api_key()
    if not api_key:
        return None

    try:
        # Prompt caching on the system prompt — it's stable across all dishes
        # so we mark it as ephemeral cache to amortize across calls.
        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": _build_user_prompt(dish, restaurant)},
        ]
        response = await litellm.acompletion(
            model=_model(),
            messages=messages,
            max_tokens=220,
            api_key=api_key,
        )
        text = response.choices[0].message.content or ""
        text = text.strip()
        return text or None
    except Exception:
        logger.exception("Dish editorial blurb generation failed (dish_id=%s)", dish.id)
        return None


async def refresh_dish_blurb(
    db: AsyncSession,
    dish_id: uuid.UUID,
    *,
    force: bool = False,
) -> bool:
    """Generate or refresh the editorial blurb for a dish.

    Returns True when the dish row was updated and committed.
    """
    if not _api_key():
        logger.debug("Editorial enricher: no API key configured — skipping.")
        return False

    row = (
        await db.execute(
            select(Dish, Restaurant)
            .join(Restaurant, Restaurant.id == Dish.restaurant_id)
            .where(Dish.id == dish_id)
        )
    ).first()
    if row is None:
        return False
    dish, restaurant = row

    if not force and dish.editorial_blurb:
        return False

    blurb = await _generate_blurb(dish, restaurant)
    if not blurb:
        return False

    dish.editorial_blurb = blurb
    dish.editorial_blurb_lang = "es"
    dish.editorial_blurb_source = "claude"
    dish.editorial_cached_at = datetime.now(timezone.utc)
    await db.commit()
    return True


async def _refresh_in_background(dish_id: uuid.UUID) -> None:
    """Open a fresh DB session — request-scoped sessions are closed already."""
    async with async_session() as session:
        try:
            await refresh_dish_blurb(session, dish_id, force=False)
        except Exception:  # pragma: no cover — background swallow
            logger.exception("Background dish blurb refresh failed (%s)", dish_id)


def maybe_schedule_blurb_refresh(
    background_tasks: BackgroundTasks, dish_id: uuid.UUID
) -> None:
    """Best-effort: enqueue blurb generation for a dish if API key is set.

    We don't pre-check the dish state here (e.g. whether a blurb already
    exists) — that check happens inside `refresh_dish_blurb` against a fresh
    session. Cheap to enqueue: the bg task short-circuits when not needed.
    """
    if not _api_key():
        return
    background_tasks.add_task(_refresh_in_background, dish_id)
