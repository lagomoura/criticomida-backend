"""Vision tagging via Gemini Flash multimodal.

Phase 2 — Ghostwriter. The user uploads a photo of a dish, and we ask
Gemini to return:

- ``tags`` — short hashtag-style descriptors the user can pin to the
  review (e.g. ``#saffron``, ``#crispy``).
- ``visible_ingredients`` — free-form list of components the model
  recognizes on the plate.
- ``plating_style`` — one of ``minimalist``, ``family-style``,
  ``deconstructed``, ``rustic``, ``classic``.
- ``editorial_blurb`` — 1-2 sentences in Palato's editorial tone.
- ``suggested_pros`` / ``suggested_cons`` — short bullets the
  reviewer can accept verbatim or edit.

The model returns a JSON object thanks to ``response_schema`` pointing
at a Pydantic model — Gemini deserializes for us into a typed object.
We still run ``_normalize_output`` afterwards to clip lengths,
lowercase tags, dedupe and reject unknown plating styles; Pydantic's
type-shape validation is layered on top of (not replacing) that
sanitization.

Failures degrade gracefully: every output field is optional in the
returned dict, so the caller always gets *something* back even if
Gemini is misconfigured, the image is unreachable, or the response
fails to parse.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.config import settings
from app.services._safe_url import UnsafeURLError, safe_fetch_bytes

logger = logging.getLogger(__name__)


# Flash is ~10x cheaper than Pro for this task and the latency is
# noticeable in a "wait for tags" UI flow. Override via env if needed.
_VISION_MODEL = "gemini-2.5-flash"
_MAX_TAGS = 8
_MAX_INGREDIENTS = 12

_PLATING_STYLES = {
    "minimalist",
    "family-style",
    "deconstructed",
    "rustic",
    "classic",
}

_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    genai_errors.APIError,
    httpx.HTTPError,
)


def _build_style_block(samples: list[str]) -> str:
    """Addendum que sólo se suma al system instruction si el usuario tiene
    reseñas previas suficientemente largas. Importante: el bloque acota su
    alcance al ``editorial_blurb`` para no contaminar tags/ingredientes/pros
    /cons, que tienen que seguir siendo observacionales sobre la foto.
    """
    numbered = "\n".join(f"{i + 1}. \"{s}\"" for i, s in enumerate(samples))
    return (
        "# Voz del autor\n\n"
        "Abajo van reseñas previas escritas por este mismo usuario. "
        "Usalas SOLO para inferir su voz al redactar `editorial_blurb`: "
        "imitá su registro (formal/informal), longitud típica de frase, "
        "vocabulario y muletillas.\n\n"
        "Reglas:\n"
        "- NO copies frases literales de las muestras.\n"
        "- NO menciones platos, restaurantes ni lugares que aparezcan en "
        "ellas (son de OTROS platos).\n"
        "- Si el autor usa clichés vacíos (\"delicioso\", \"exquisito\", "
        "\"una explosión de sabor\"), priorizá las reglas originales de "
        "Palato por sobre la imitación.\n"
        "- Para `tags`, `visible_ingredients`, `plating_style`, "
        "`suggested_pros` y `suggested_cons`: ignorá estas muestras "
        "completamente y seguí las reglas originales (observacional sobre "
        "la foto).\n\n"
        "Reseñas previas (más recientes primero):\n"
        f"{numbered}"
    )


_SYSTEM_INSTRUCTION = """Sos el Ghostwriter de Palato. Mirás fotos de platos y proponés etiquetas + texto editorial corto, en español rioplatense, tono cálido y específico (sin clichés tipo "delicioso", "exquisito", "una explosión de sabor").

Devolvé SIEMPRE un JSON válido con esta forma exacta:

{
  "tags": ["palabra1", "palabra2"],          // EXACTAMENTE 3-6 tags, lowercase, sin "#", sin espacios
  "visible_ingredients": ["arroz", "azafrán"], // MÁX 6 ingredientes únicos, lowercase, en español
  "plating_style": "minimalist|family-style|deconstructed|rustic|classic",
  "editorial_blurb": "Frase 1. Frase 2.",    // OBLIGATORIO. 1-2 frases, MÁX 200 caracteres
  "suggested_pros": ["punto 1", "punto 2"],  // OBLIGATORIO 1-2 ítems, ≤60 caracteres c/u
  "suggested_cons": ["punto 1"]              // OPCIONAL 0-2 ítems, ≤60 caracteres c/u
}

Reglas innegociables:
- `editorial_blurb`, `tags` y `suggested_pros` NUNCA pueden ir vacíos: dale algo concreto basado en lo que efectivamente ves (textura, dorado, cantidad, presentación). Si no podés identificar el plato exacto, escribí sobre lo visual.
- `suggested_cons` puede ir vacío SOLO si la foto no muestra ningún detalle observable que justifique una crítica (no inventes contras genéricas tipo "podría tener más sabor" — eso no se ve).
- NO enumeres variantes del mismo ingrediente (un único "caldo", no "caldo de pollo / de carne / de pescado").
- NO repitas conceptos entre tags e ingredientes.
- Pros/contras tienen que ser observables en la foto: "queso bien fundido", "porción generosa", "presentación apretada", "salsa escasa". Nada que requiera probar el plato.
- La respuesta entera tiene que ser JSON válido y compacto.
"""


class _VisionSchema(BaseModel):
    """Wire shape Gemini fills in. We keep the field types permissive
    (plain lists of strings) — bounds (min/max items, character caps,
    plating-style enum) are enforced post-parse in ``_normalize_output``
    so a single off-by-one violation from the model doesn't invalidate
    the entire response."""

    tags: list[str] = Field(default_factory=list)
    visible_ingredients: list[str] = Field(default_factory=list)
    plating_style: str | None = None
    editorial_blurb: str | None = None
    suggested_pros: list[str] = Field(default_factory=list)
    suggested_cons: list[str] = Field(default_factory=list)


class VisionUnavailable(RuntimeError):
    """Raised only when callers explicitly request strict mode."""


_client: genai.Client | None = None


def _get_client() -> genai.Client | None:
    global _client
    key = settings.GEMINI_API_KEY
    if not key:
        return None
    if _client is None:
        _client = genai.Client(api_key=key)
    return _client


async def _fetch_image(url: str) -> tuple[bytes, str]:
    """Download an image and return (bytes, mime). Times out fast: a
    review-creation flow can't wait 30s for a slow CDN.

    SSRF-hardened via ``safe_fetch_bytes``: scheme allowlist + DNS
    rejection of private/loopback/link-local/metadata IPs + redirects
    disabled + 16 MB response cap.
    """
    return await safe_fetch_bytes(url, timeout=10.0)


def _empty_response() -> dict[str, Any]:
    return {
        "tags": [],
        "visible_ingredients": [],
        "plating_style": None,
        "editorial_blurb": None,
        "suggested_pros": [],
        "suggested_cons": [],
    }


def _normalize_output(raw: _VisionSchema) -> dict[str, Any]:
    """Clamp and sanitize the model output. The vision model is mostly
    well-behaved but we never trust its bounds: dedupe tags, lowercase,
    clip lengths per field, drop unknown plating styles."""
    tags = [
        t.strip().lower().replace(" ", "-").lstrip("#")
        for t in raw.tags
        if isinstance(t, str)
    ]
    seen: set[str] = set()
    deduped: list[str] = []
    for t in tags:
        if not t or t in seen:
            continue
        seen.add(t)
        deduped.append(t[:40])
        if len(deduped) >= _MAX_TAGS:
            break

    ingredients = [
        i.strip().lower()
        for i in raw.visible_ingredients
        if isinstance(i, str) and i.strip()
    ][:_MAX_INGREDIENTS]

    plating_style = raw.plating_style
    if isinstance(plating_style, str):
        plating_style = plating_style.strip().lower()
        if plating_style not in _PLATING_STYLES:
            plating_style = None
    else:
        plating_style = None

    blurb = raw.editorial_blurb
    if isinstance(blurb, str):
        blurb = blurb.strip()[:240] or None
    else:
        blurb = None

    def _clip_list(items: list[str], n: int, char_cap: int) -> list[str]:
        out: list[str] = []
        for x in items:
            if not isinstance(x, str):
                continue
            x = x.strip()
            if not x:
                continue
            out.append(x[:char_cap])
            if len(out) >= n:
                break
        return out

    return {
        "tags": deduped,
        "visible_ingredients": ingredients,
        "plating_style": plating_style,
        "editorial_blurb": blurb,
        "suggested_pros": _clip_list(raw.suggested_pros, 3, 80),
        "suggested_cons": _clip_list(raw.suggested_cons, 2, 80),
    }


async def analyze_dish_photo(
    *,
    photo_url: str | None = None,
    photo_bytes: bytes | None = None,
    photo_mime: str | None = None,
    dish_hint: str | None = None,
    style_samples: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze a dish photo and return Ghostwriter suggestions.

    Pass either ``photo_url`` (we'll fetch it) or ``photo_bytes`` +
    ``photo_mime`` (already in memory, e.g. from a multipart upload).
    ``dish_hint`` lets the caller pass the dish name so the model can
    bias tags toward what it expects (helps disambiguate: "pizza" with
    a hint of "fugazzeta" vs "napolitana" gives different tags).
    ``style_samples`` son notas de reseñas previas del autor; cuando se
    pasan, el ``editorial_blurb`` imita su voz (ver ``_build_style_block``).
    """
    client = _get_client()
    if client is None:
        return _empty_response()

    if photo_bytes is None and photo_url:
        try:
            photo_bytes, photo_mime = await _fetch_image(photo_url)
        except UnsafeURLError as exc:
            logger.warning("Rejected photo URL %s: %s", photo_url, exc)
            return _empty_response()
        except httpx.HTTPError as exc:
            logger.warning("Couldn't fetch photo %s: %s", photo_url, exc)
            return _empty_response()

    if not photo_bytes:
        return _empty_response()

    text_prompt = "Analizá la foto del plato y devolvé el JSON pedido."
    if dish_hint:
        text_prompt += f" Pista: el plato probablemente es '{dish_hint}'."

    system_text = _SYSTEM_INSTRUCTION
    if style_samples:
        system_text = f"{system_text}\n\n{_build_style_block(style_samples)}"

    image_part = genai_types.Part.from_bytes(
        data=photo_bytes, mime_type=photo_mime or "image/jpeg"
    )
    contents = [
        genai_types.Content(
            role="user",
            parts=[image_part, genai_types.Part.from_text(text=text_prompt)],
        )
    ]
    config = genai_types.GenerateContentConfig(
        system_instruction=system_text,
        response_mime_type="application/json",
        response_schema=_VisionSchema,
        temperature=0.4,
        # 800 was tight: a dish with many ingredients + blurb + pros
        # + cons can blow past it and we'd get truncated JSON.
        max_output_tokens=2048,
    )

    try:
        response = await client.aio.models.generate_content(
            model=_VISION_MODEL,
            contents=contents,
            config=config,
        )
    except _PROVIDER_ERRORS as exc:
        logger.warning("Gemini vision call failed: %s", exc)
        return _empty_response()

    parsed = response.parsed
    if not isinstance(parsed, _VisionSchema):
        # Either the SDK couldn't deserialize (schema mismatch, partial
        # JSON from MAX_TOKENS) or the response had no candidates.
        finish_reason = None
        try:
            finish_reason = response.candidates[0].finish_reason  # type: ignore[index]
        except (AttributeError, IndexError):
            pass
        logger.warning(
            "Gemini vision returned unparseable payload "
            "(finishReason=%s, parsed=%r)",
            finish_reason,
            type(parsed).__name__,
        )
        return _empty_response()

    return _normalize_output(parsed)
