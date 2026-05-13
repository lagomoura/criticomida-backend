"""Category inference for new posts via Gemini Flash 2.5.

Antes el FE pedía al usuario que pickeara la categoría del plato en
`/compose` (52-item ``<Select>``). Ese paso se eliminó: ahora el backend
infiere la categoría a partir del nombre del plato + contexto del
restaurante + el origen/blurb editorial que ya generó el Editorial
Enricher. Esta capa es invocada solo cuando:

1. El FE no manda `payload.category` (caso esperado tras el cambio).
2. El restaurante resuelto en el request **no tiene** `category_id`
   asignado todavía (respetamos clasificaciones previas).

Output: ``(category_id, was_newly_created)``. El router usa eso para:

- Setear ``restaurant.category_id`` con el id devuelto.
- Si ``was_newly_created`` → disparar
  ``admin_notification_service.notify_admins_category_pending`` (la
  nueva categoría queda con ``pending_review = True`` hasta que el
  admin la cura).

Patrón de Gemini calcado de ``sentiment_service.build_sentiment_config``:
``response_schema`` Pydantic + ``thinking_budget=0`` (memoria
``feedback_gemini_thinking`` — sin esto Flash 2.5 trunca JSON-mode
corto). Modelo: ``gemini-2.5-flash`` por consistencia con sentiment y
ghostwriter; ``temperature=0.1`` para que la elección sea estable
entre runs sobre el mismo plato.

Cuando ``GEMINI_API_KEY`` está vacío (o la API falla), devolvemos el id
de ``otros`` como fallback duro para que el post nunca quede con
``category_id = NULL``. El servicio nunca tira excepción al caller —
el peor caso es ``(otros.id, False)``.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.category import Category

logger = logging.getLogger(__name__)


INFERENCE_MODEL = "gemini-2.5-flash"

# Confianza mínima para aceptar la elección de Gemini sobre una
# categoría existente. Por debajo de esto preferimos crear una pendiente
# nueva (o caer a `otros` si Gemini tampoco propone). 0.6 se eligió
# alineado con el threshold de ``resolve_entidades`` (memoria
# ``feedback_resolve_entidades``): suficientemente alto para que el
# admin no se llene de ruido, suficientemente bajo para que platos
# obvios (sushi, pizza) no caigan a `otros`.
EXISTING_PICK_MIN_CONFIDENCE = 0.6

# Cap de la propuesta de nombre/description del nuevo slug — las
# columnas en DB son VARCHAR(100) y VARCHAR(500).
_MAX_NAME_LEN = 100
_MAX_DESCRIPTION_LEN = 500

# Slug del fallback. Existe desde la migration 008 y la 047 lo deja en
# display_order=999.
_FALLBACK_SLUG = "otros"


_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    genai_errors.APIError,
    httpx.HTTPError,
)


_SYSTEM_INSTRUCTION = """Sos un clasificador gastronómico para una red social de reseñas (es-AR / pt-BR / en).

Recibís el nombre de un plato, el restaurante donde se sirve y, si están disponibles, el origen y la breve historia editorial del plato. Tu trabajo es asignarle UNA categoría.

Devolvé SIEMPRE un JSON válido con esta forma exacta:

{
  "existing": { "slug": "<slug-de-la-lista>", "confidence": <0.0-1.0> } | null,
  "proposed_new": { "slug": "<nuevo-slug>", "name": "<nombre humano>", "description": "<1 línea + 4-6 platos típicos>", "reasoning": "<por qué ninguna existente encaja>" } | null
}

Reglas (en orden de prioridad):

1. **Preferí SIEMPRE una categoría existente** cuando el plato encaja con confianza ≥ 0.6. Devolvé `existing` y dejá `proposed_new` en null.
2. Si dudás entre dos existentes (ej: 'italiana' vs 'pizzeria-hipotetica'), elegí la MÁS GENERAL que ya exista. NO propongas un slug nuevo solo porque sería más específico.
3. Si NINGUNA existente encaja razonablemente (confianza máxima < 0.6), devolvé `existing = null` y proponé una nueva en `proposed_new`. Reglas del slug propuesto:
   - kebab-case, lowercase, ASCII sin tildes. Ej: 'georgiana', 'etiope', 'fusion-nikkei'.
   - El `name` es la versión humana en español rioplatense (ej: 'Georgiana', 'Etíope').
   - La `description` arranca con un rasgo distintivo + 4-6 platos típicos. Mismo formato que las descripciones de la lista.
   - `reasoning`: una frase explicando por qué las existentes no servían.
4. NUNCA propongas un slug que ya esté en la lista (ni siquiera con variaciones tipo 'italian' vs 'italiana').
5. Si el plato es ambiguo (ej: 'agua mineral', 'café') usá `existing` apuntando a la categoría más razonable del contexto (cafetería, bar) o, último recurso, 'otros'.
6. Devolvé el JSON pelado, sin texto extra ni comentarios."""


class _ExistingPick(BaseModel):
    slug: str = Field(min_length=1, max_length=100)
    confidence: float = Field(ge=0.0, le=1.0)


class _NewCategoryProposal(BaseModel):
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=_MAX_NAME_LEN)
    description: str = Field(min_length=1, max_length=_MAX_DESCRIPTION_LEN)
    reasoning: str = Field(min_length=1, max_length=400)


class _CategoryInferenceResponse(BaseModel):
    """Wire schema. Gemini puede devolver ambos null en casos raros — el
    parser lo trata como fallback duro a `otros`. Pydantic no puede
    enforcear ``Optional[A] | Optional[B]`` con XOR semántico."""

    existing: _ExistingPick | None = None
    proposed_new: _NewCategoryProposal | None = None


@dataclass(frozen=True)
class CategoryInferenceResult:
    category_id: int
    slug: str
    was_newly_created: bool


_client: genai.Client | None = None


def _get_client() -> genai.Client | None:
    global _client
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    if _client is None:
        _client = genai.Client(api_key=key)
    return _client


def _slugify(value: str) -> str:
    """ASCII-only, lowercase, kebab-case. Sin tildes, sin ñ → 'n'.

    Gemini puede devolver 'Italiana' o 'fusión-nikkei'. Normalizamos
    para que el chequeo de colisión vs DB y la unicidad del slug sean
    deterministas. Caracteres no [a-z0-9-] se colapsan a un único `-`.
    """
    nfkd = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = ascii_only.lower().strip()
    slug = re.sub(r"[^a-z0-9-]+", "-", lowered)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:100]


def _build_user_prompt(
    *,
    dish_name: str,
    restaurant_name: str | None,
    restaurant_category_slug: str | None,
    dish_editorial_origin: str | None,
    dish_editorial_blurb: str | None,
    existing_categories: list[Category],
) -> str:
    lines: list[str] = []
    lines.append(f"Plato: {dish_name.strip()}")
    if restaurant_name:
        lines.append(f"Restaurante: {restaurant_name.strip()}")
    if restaurant_category_slug:
        lines.append(
            f"Slug de categoría inferida por Google Places (pista, puede estar mal): {restaurant_category_slug.strip()}"
        )
    if dish_editorial_origin:
        lines.append(f"Origen editorial del plato: {dish_editorial_origin.strip()}")
    if dish_editorial_blurb:
        lines.append(f"Historia breve: {dish_editorial_blurb.strip()}")

    lines.append("")
    lines.append("Categorías existentes (slug — name — description):")
    for cat in existing_categories:
        desc = (cat.description or "(sin descripción)").strip()
        lines.append(f"- {cat.slug} — {cat.name} — {desc}")

    return "\n".join(lines)


def _build_config() -> genai_types.GenerateContentConfig:
    return genai_types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=_CategoryInferenceResponse,
        temperature=0.1,
        # Memoria ``feedback_gemini_thinking``: sin esto Flash 2.5 trunca
        # el JSON corto y baja la tasa de éxito a single-digit %.
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=512,
    )


async def _load_approved_categories(db: AsyncSession) -> list[Category]:
    """Solo categorías no-pending: las pendientes no entran al prompt
    porque todavía no son ground truth. El espacio total queda < 60
    rows así que no necesitamos cache — Postgres lo resuelve en <5 ms.
    """
    result = await db.execute(
        select(Category)
        .where(Category.pending_review.is_(False))
        .order_by(Category.display_order, Category.name)
    )
    return list(result.scalars().all())


async def _get_category_by_slug(
    db: AsyncSession, slug: str
) -> Category | None:
    result = await db.execute(select(Category).where(Category.slug == slug))
    return result.scalar_one_or_none()


def _parse_response(
    response: genai_types.GenerateContentResponse | None,
) -> _CategoryInferenceResponse | None:
    """Calcado de ``parse_sentiment_response``: probamos ``.parsed``
    primero, fallback a parsear el texto. Loggea y devuelve None si la
    forma no encaja."""
    if response is None:
        return None
    raw_parsed = response.parsed
    if isinstance(raw_parsed, _CategoryInferenceResponse):
        return raw_parsed
    text = (response.text or "").strip()
    if not text:
        logger.warning("Gemini category inference empty response")
        return None
    try:
        return _CategoryInferenceResponse.model_validate_json(text)
    except ValueError as exc:
        logger.warning(
            "Gemini category inference unparseable JSON (tail=%r, err=%s)",
            text[-120:],
            exc,
        )
        return None


async def _fallback_otros(db: AsyncSession) -> CategoryInferenceResult:
    cat = await _get_category_by_slug(db, _FALLBACK_SLUG)
    if cat is None:
        # Defensa en profundidad: si por alguna razón 'otros' no existe,
        # devolvemos el primer slug aprobado (orden estable por display
        # order). Mejor un id válido cualquiera que romper el post.
        result = await db.execute(
            select(Category)
            .where(Category.pending_review.is_(False))
            .order_by(Category.display_order, Category.id)
            .limit(1)
        )
        cat = result.scalar_one_or_none()
        if cat is None:
            raise RuntimeError(
                "No hay ninguna categoría aprobada en DB — seed corrompida"
            )
    return CategoryInferenceResult(
        category_id=cat.id, slug=cat.slug, was_newly_created=False
    )


async def infer_category(
    db: AsyncSession,
    *,
    dish_name: str,
    restaurant_name: str | None = None,
    restaurant_category_slug: str | None = None,
    dish_editorial_origin: str | None = None,
    dish_editorial_blurb: str | None = None,
) -> CategoryInferenceResult:
    """Punto de entrada único del servicio.

    Devuelve siempre un ``CategoryInferenceResult`` válido. Nunca tira:
    el peor caso es el id de ``otros`` con ``was_newly_created=False``.
    El caller decide qué hacer con ``was_newly_created`` (típicamente:
    disparar la notificación a admins).
    """
    cleaned_name = (dish_name or "").strip()
    if not cleaned_name:
        return await _fallback_otros(db)

    client = _get_client()
    if client is None:
        logger.info("category_inference skipped: GEMINI_API_KEY unset")
        return await _fallback_otros(db)

    existing_categories = await _load_approved_categories(db)
    if not existing_categories:
        return await _fallback_otros(db)

    prompt = _build_user_prompt(
        dish_name=cleaned_name,
        restaurant_name=restaurant_name,
        restaurant_category_slug=restaurant_category_slug,
        dish_editorial_origin=dish_editorial_origin,
        dish_editorial_blurb=dish_editorial_blurb,
        existing_categories=existing_categories,
    )

    try:
        response = await client.aio.models.generate_content(
            model=INFERENCE_MODEL,
            contents=prompt,
            config=_build_config(),
        )
    except _PROVIDER_ERRORS as exc:
        logger.warning("Gemini category inference call failed: %s", exc)
        return await _fallback_otros(db)

    parsed = _parse_response(response)
    if parsed is None:
        return await _fallback_otros(db)

    existing_slugs = {c.slug for c in existing_categories}

    # Rama A: Gemini eligió una existente con confianza suficiente.
    if parsed.existing is not None:
        picked_slug = parsed.existing.slug.strip().lower()
        if (
            picked_slug in existing_slugs
            and parsed.existing.confidence >= EXISTING_PICK_MIN_CONFIDENCE
        ):
            cat = await _get_category_by_slug(db, picked_slug)
            if cat is not None:
                logger.info(
                    "category_inferred slug=%s confidence=%.2f dish=%r",
                    cat.slug,
                    parsed.existing.confidence,
                    cleaned_name,
                )
                return CategoryInferenceResult(
                    category_id=cat.id,
                    slug=cat.slug,
                    was_newly_created=False,
                )

    # Rama B: Gemini propone nueva. Validamos slug + colisión.
    if parsed.proposed_new is not None:
        proposal = parsed.proposed_new
        normalized = _slugify(proposal.slug)
        if not normalized:
            logger.warning(
                "category_inference rejected proposal: empty slug after slugify (raw=%r)",
                proposal.slug,
            )
            return await _fallback_otros(db)
        if normalized in existing_slugs:
            # El modelo "propuso nueva" pero el slug colisiona con una
            # existente. Lo tratamos como pick implícito de esa existente.
            cat = await _get_category_by_slug(db, normalized)
            if cat is not None:
                logger.info(
                    "category_inferred slug=%s (proposed_new collapsed to existing) dish=%r",
                    cat.slug,
                    cleaned_name,
                )
                return CategoryInferenceResult(
                    category_id=cat.id,
                    slug=cat.slug,
                    was_newly_created=False,
                )
        # Crear pending. ``display_order=999`` igual que 'otros' para que
        # quede al final hasta que el admin la cure.
        new_cat = Category(
            slug=normalized,
            name=proposal.name.strip()[:_MAX_NAME_LEN],
            description=proposal.description.strip()[:_MAX_DESCRIPTION_LEN],
            display_order=999,
            pending_review=True,
        )
        db.add(new_cat)
        await db.flush()
        logger.info(
            "category_inferred created_new slug=%s name=%r dish=%r reasoning=%r",
            new_cat.slug,
            new_cat.name,
            cleaned_name,
            proposal.reasoning,
        )
        return CategoryInferenceResult(
            category_id=new_cat.id,
            slug=new_cat.slug,
            was_newly_created=True,
        )

    # Rama C: Gemini devolvió ambos en null o pick con baja confianza —
    # fallback duro.
    logger.info(
        "category_inference fell back to '%s' (no confident pick) dish=%r",
        _FALLBACK_SLUG,
        cleaned_name,
    )
    return await _fallback_otros(db)
