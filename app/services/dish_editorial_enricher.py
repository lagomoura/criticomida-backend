"""Dish editorial blurb enrichment via LLM.

Genera una mini cápsula editorial sobre cada plato — origen + curiosidad
cultural — sin referencia al restaurante específico. Persiste:

- `editorial_origin`: etiqueta corta de la cocina/tradición ("Cocina napolitana")
- `editorial_blurb`: 2-3 oraciones con la historia/curiosidad del plato

El servicio degrada silenciosamente cuando no hay API key configurada. La
generación corre como background task al abrir el detalle del plato y
persiste con `EDITORIAL_PROMPT_VERSION`; cuando cambia la versión, los
blurbs viejos se consideran stale y se regeneran (lazy y/o vía script).
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chat.agent_loop import strip_provider_prefix

from app.database import async_session
from app.models.dish import Dish, DishEditorialCache
from app.models.restaurant import Restaurant

logger = logging.getLogger(__name__)


# Bump cuando cambia el prompt o el shape de salida — el script de backfill y
# el trigger lazy detectan stale por mismatch contra `dish.editorial_prompt_version`.
EDITORIAL_PROMPT_VERSION = "v2"


SYSTEM_PROMPT = (
    "Eres un editor gastronómico para Palato. Te dan el nombre de un "
    "plato y la cocina a la que pertenece. Devolvé un JSON con dos campos:\n"
    '  "origin": etiqueta corta (máx. 5 palabras) que ubique al plato en su '
    "tradición. Ejemplos: \"Cocina napolitana\", \"Sushi · Edo, Japón\", "
    "\"Asado rioplatense\", \"Cocina andaluza\".\n"
    '  "story": 2 a 3 oraciones (máx. 60 palabras) en español rioplatense '
    "neutro que cuenten brevemente el origen del plato y una curiosidad "
    "concreta — un ingrediente clave, una técnica tradicional, una anécdota "
    "cultural o el momento histórico en que apareció.\n"
    "Tono editorial, evocativo pero sobrio. NO menciones el restaurante ni "
    "el local. No uses listas, emojis, hashtags ni signos de exclamación. "
    "No inventes datos: si no estás seguro, mantenete en conocimiento "
    "general del plato."
)


def _model() -> str:
    raw = os.getenv(
        "EDITORIAL_MODEL",
        os.getenv("CHAT_MODEL", "gemini-3.1-flash-lite-preview"),
    )
    return strip_provider_prefix(raw)


def _api_key() -> str | None:
    return (
        os.getenv("EDITORIAL_API_KEY")
        or os.getenv("CHAT_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )


class _EditorialSchema(BaseModel):
    """Wire shape Gemini fills in. Both fields stay permissive — the
    cleanup (length cap, blank rejection) lives in ``_normalize_blurb``
    so that a minor formatting wobble doesn't invalidate the entire
    response."""

    origin: str | None = None
    story: str | None = None


def _build_user_prompt(dish: Dish, restaurant: Restaurant) -> str:
    parts = [f"Plato: {dish.name}"]
    if restaurant.cuisine_types:
        parts.append(f"Cocina: {', '.join(restaurant.cuisine_types[:6])}")
    if dish.description:
        parts.append(f"Descripción del plato: {dish.description[:400]}")
    parts.append(
        "Devolvé el JSON con `origin` y `story` siguiendo las instrucciones "
        "del sistema."
    )
    return "\n".join(parts)


def _normalize_blurb(parsed: _EditorialSchema) -> tuple[str, str | None] | None:
    """Validate and clean the parsed response. Returns ``(story, origin)``
    or ``None`` when the story is missing — without a story there is no
    blurb to show."""
    story = (parsed.story or "").strip()
    if not story:
        return None
    origin = (parsed.origin or "").strip() or None
    if origin and len(origin) > 80:
        origin = origin[:80]
    return story, origin


async def _generate_blurb(
    dish: Dish, restaurant: Restaurant
) -> tuple[str, str | None] | None:
    api_key = _api_key()
    if not api_key:
        return None

    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=_model(),
            contents=[
                genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=_build_user_prompt(dish, restaurant))],
                )
            ],
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=320,
                response_mime_type="application/json",
                response_schema=_EditorialSchema,
                # Trivial classification — el thinking no aporta y
                # consume budget; lo apagamos para que la latencia no
                # vuele cuando se piden varios blurbs en paralelo.
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, _EditorialSchema):
            logger.warning(
                "Editorial blurb parse failed (dish_id=%s, parsed=%r)",
                dish.id,
                type(parsed).__name__,
            )
            return None
        return _normalize_blurb(parsed)
    except Exception:
        logger.exception("Dish editorial blurb generation failed (dish_id=%s)", dish.id)
        return None


def _is_stale(dish: Dish) -> bool:
    """True si el blurb es viejo, falta, o se generó con un prompt anterior."""
    if not dish.editorial_blurb:
        return True
    if dish.editorial_prompt_version != EDITORIAL_PROMPT_VERSION:
        return True
    return False


def _cuisine_key(restaurant: Restaurant) -> str:
    """Clave estable de cocina para la cache compartida.

    Tomamos la primera cocina **alfabéticamente** (no la primera del array)
    para que el orden en `cuisine_types` no parta la cache: un restaurante
    cargado como ["italiana", "argentina"] y otro como ["argentina", "italiana"]
    convergen al mismo key. El dish_name normalizado ya diferencia
    "tortilla española" de "tortilla mexicana" en la mayoría de los casos.
    Sin cuisines → key vacía: el blurb se cachea como "neutro".
    """
    if not restaurant.cuisine_types:
        return ""
    cleaned = sorted(
        c.strip().lower() for c in restaurant.cuisine_types if c and c.strip()
    )
    if not cleaned:
        return ""
    return cleaned[0][:80]


def _normalize_name_fallback(name: str) -> str:
    """Reproduce `public.dish_name_normalized(name)` en Python.

    SQL: lower(unaccent(regexp_replace(trim($1), '\\s+', ' ', 'g'))). Lo usamos
    como fallback cuando `dish.name_normalized` viene vacío (p. ej. dishes
    insertados antes del trigger o por un edge case del normalizador).
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


async def _read_cache(
    db: AsyncSession, name_key: str, cuisine_key: str
) -> tuple[str, str | None] | None:
    row = await db.execute(
        select(DishEditorialCache).where(
            DishEditorialCache.name_key == name_key,
            DishEditorialCache.cuisine_key == cuisine_key,
            DishEditorialCache.prompt_version == EDITORIAL_PROMPT_VERSION,
        )
    )
    cached = row.scalar_one_or_none()
    if cached is None:
        return None
    return cached.story, cached.origin


async def _write_cache(
    db: AsyncSession,
    name_key: str,
    cuisine_key: str,
    story: str,
    origin: str | None,
) -> None:
    """Upsert con `ON CONFLICT DO UPDATE` para race-safety entre refreshes concurrentes."""
    stmt = pg_insert(DishEditorialCache).values(
        name_key=name_key,
        cuisine_key=cuisine_key,
        story=story,
        origin=origin,
        prompt_version=EDITORIAL_PROMPT_VERSION,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="pk_dish_editorial_cache",
        set_={
            "story": stmt.excluded.story,
            "origin": stmt.excluded.origin,
            "prompt_version": stmt.excluded.prompt_version,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)


async def refresh_dish_blurb(
    db: AsyncSession,
    dish_id: uuid.UUID,
    *,
    force: bool = False,
) -> bool:
    """Genera o refresca el blurb editorial del plato.

    Flujo:
      1. Si el dish ya está al día y no es `force`, no hacemos nada.
      2. Lookup en `dish_editorial_cache` por (name_key, cuisine_key) — si
         hit, copiamos al dish sin llamar al LLM.
      3. Miss: una llamada al LLM, escribimos cache + dish.

    Devuelve True cuando el row del dish se actualizó y commiteó.
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

    if not force and not _is_stale(dish):
        return False

    name_key = (dish.name_normalized or "").strip()
    if not name_key:
        name_key = _normalize_name_fallback(dish.name or "")
    cuisine_key = _cuisine_key(restaurant)

    if name_key:
        cached = await _read_cache(db, name_key, cuisine_key)
        if cached is not None:
            story, origin = cached
            _apply_blurb(dish, story, origin)
            await db.commit()
            return True

    result = await _generate_blurb(dish, restaurant)
    if not result:
        return False
    story, origin = result

    if name_key:
        await _write_cache(db, name_key, cuisine_key, story, origin)
    _apply_blurb(dish, story, origin)
    await db.commit()
    return True


def _apply_blurb(dish: Dish, story: str, origin: str | None) -> None:
    dish.editorial_blurb = story
    dish.editorial_origin = origin
    dish.editorial_blurb_lang = "es"
    dish.editorial_blurb_source = "gemini"
    dish.editorial_prompt_version = EDITORIAL_PROMPT_VERSION
    dish.editorial_cached_at = datetime.now(timezone.utc)


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

    El check de stale corre dentro de `refresh_dish_blurb` con una sesión
    fresca, así que enqueue es barato — el task hace short-circuit cuando
    el blurb actual ya está al día con `EDITORIAL_PROMPT_VERSION`.
    """
    if not _api_key():
        return
    background_tasks.add_task(_refresh_in_background, dish_id)
