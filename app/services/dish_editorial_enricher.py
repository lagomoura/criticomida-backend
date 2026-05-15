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


# Idiomas soportados por la UI (next-intl: es/en/pt). El default es `es`
# porque es el idioma histórico de la cache y el canónico del dish row.
SUPPORTED_LANGS = ("es", "en", "pt")
DEFAULT_LANG = "es"

# En qué idioma debe redactarse `story`, por código. Las *instrucciones*
# siguen en español (el modelo las obedece igual); lo único que cambia es
# el idioma de salida exigido y el matiz regional.
_STORY_LANGUAGE = {
    "es": "español rioplatense neutro",
    "en": "neutral, editorial English",
    "pt": "português do Brasil neutro",
}


def normalize_lang(lang: str | None) -> str:
    """Mapea cualquier locale entrante al set soportado; cae a `es`."""
    if not lang:
        return DEFAULT_LANG
    code = lang.strip().lower()[:2]
    return code if code in SUPPORTED_LANGS else DEFAULT_LANG


def _system_prompt(lang: str) -> str:
    story_lang = _STORY_LANGUAGE[lang]
    return (
        "Eres un editor gastronómico para Palato. Te dan el nombre de un "
        "plato y la cocina a la que pertenece. Devolvé un JSON con dos campos:\n"
        '  "origin": etiqueta corta (máx. 5 palabras) que ubique al plato en su '
        "tradición. Ejemplos: \"Cocina napolitana\", \"Sushi · Edo, Japón\", "
        "\"Asado rioplatense\", \"Cocina andaluza\". Escribí `origin` en "
        f"{story_lang}.\n"
        '  "story": 2 a 3 oraciones (máx. 60 palabras) en '
        f"{story_lang} que cuenten brevemente el origen del plato y una "
        "curiosidad concreta — un ingrediente clave, una técnica tradicional, "
        "una anécdota cultural o el momento histórico en que apareció.\n"
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
    dish: Dish, restaurant: Restaurant, lang: str
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
                system_instruction=_system_prompt(lang),
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
    db: AsyncSession, name_key: str, cuisine_key: str, lang: str
) -> tuple[str, str | None] | None:
    row = await db.execute(
        select(DishEditorialCache).where(
            DishEditorialCache.name_key == name_key,
            DishEditorialCache.cuisine_key == cuisine_key,
            DishEditorialCache.lang == lang,
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
    lang: str,
    story: str,
    origin: str | None,
) -> None:
    """Upsert con `ON CONFLICT DO UPDATE` para race-safety entre refreshes concurrentes."""
    stmt = pg_insert(DishEditorialCache).values(
        name_key=name_key,
        cuisine_key=cuisine_key,
        lang=lang,
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


def _keys(dish: Dish, restaurant: Restaurant) -> tuple[str, str]:
    name_key = (dish.name_normalized or "").strip()
    if not name_key:
        name_key = _normalize_name_fallback(dish.name or "")
    return name_key, _cuisine_key(restaurant)


async def get_cached_blurb(
    db: AsyncSession, dish: Dish, restaurant: Restaurant, lang: str
) -> tuple[str, str | None] | None:
    """Lookup síncrono del blurb cacheado para `lang` (sin tocar el LLM).

    Lo usa el endpoint de detalle para servir la historia en el idioma de
    la URL: si hay hit devolvemos esa historia; si no, el endpoint cae a la
    historia ES del dish row (solo cuando `lang == 'es'`) y encola la
    generación del idioma faltante en background.
    """
    lang = normalize_lang(lang)
    name_key, cuisine_key = _keys(dish, restaurant)
    if not name_key:
        return None
    return await _read_cache(db, name_key, cuisine_key, lang)


async def refresh_dish_blurb(
    db: AsyncSession,
    dish_id: uuid.UUID,
    *,
    lang: str = DEFAULT_LANG,
    force: bool = False,
) -> bool:
    """Genera o refresca el blurb editorial del plato en `lang`.

    Flujo:
      1. ES tiene un fast-path: el dish row es el store canónico, así que si
         no está stale y no es `force`, no hacemos nada.
      2. Lookup en `dish_editorial_cache` por (name_key, cuisine_key, lang).
         Hit: para ES copiamos al dish row; para el resto la cache ya alcanza
         (el endpoint la lee directo) y no hay nada que hacer.
      3. Miss: una llamada al LLM en `lang`, escribimos cache (+ dish row si ES).

    Devuelve True cuando algo se generó/copió y commiteó.
    """
    if not _api_key():
        logger.debug("Editorial enricher: no API key configured — skipping.")
        return False

    lang = normalize_lang(lang)

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

    # Fast-path ES: el dish row es el canónico. Si está al día, nada que hacer.
    if lang == DEFAULT_LANG and not force and not _is_stale(dish):
        return False

    name_key, cuisine_key = _keys(dish, restaurant)

    if not force and name_key:
        cached = await _read_cache(db, name_key, cuisine_key, lang)
        if cached is not None:
            if lang == DEFAULT_LANG:
                story, origin = cached
                _apply_blurb(dish, story, origin)
                await db.commit()
                return True
            # Non-ES ya cacheado: el endpoint lo lee directo de la cache.
            return False

    result = await _generate_blurb(dish, restaurant, lang)
    if not result:
        return False
    story, origin = result

    if name_key:
        await _write_cache(db, name_key, cuisine_key, lang, story, origin)
    if lang == DEFAULT_LANG:
        _apply_blurb(dish, story, origin)
    await db.commit()
    return True


def _apply_blurb(dish: Dish, story: str, origin: str | None) -> None:
    """Mirror del blurb ES sobre el dish row.

    Solo se llama para ES: el dish row es el store canónico español que leen
    los metadatos de página y cualquier otro consumidor. Los idiomas no-ES
    viven exclusivamente en `dish_editorial_cache`.
    """
    dish.editorial_blurb = story
    dish.editorial_origin = origin
    dish.editorial_blurb_lang = DEFAULT_LANG
    dish.editorial_blurb_source = "gemini"
    dish.editorial_prompt_version = EDITORIAL_PROMPT_VERSION
    dish.editorial_cached_at = datetime.now(timezone.utc)


async def _refresh_in_background(dish_id: uuid.UUID, lang: str) -> None:
    """Open a fresh DB session — request-scoped sessions are closed already."""
    async with async_session() as session:
        try:
            await refresh_dish_blurb(session, dish_id, lang=lang, force=False)
        except Exception:  # pragma: no cover — background swallow
            logger.exception("Background dish blurb refresh failed (%s)", dish_id)


def maybe_schedule_blurb_refresh(
    background_tasks: BackgroundTasks,
    dish_id: uuid.UUID,
    lang: str = DEFAULT_LANG,
) -> None:
    """Best-effort: enqueue blurb generation for a dish+lang if API key is set.

    El check de stale corre dentro de `refresh_dish_blurb` con una sesión
    fresca, así que enqueue es barato — el task hace short-circuit cuando
    el blurb actual ya está al día con `EDITORIAL_PROMPT_VERSION`.
    """
    if not _api_key():
        return
    background_tasks.add_task(_refresh_in_background, dish_id, normalize_lang(lang))
