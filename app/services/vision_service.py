"""Vision tagging via Gemini Flash multimodal.

Phase 2 — Ghostwriter. The user uploads a photo of a dish, and we ask
Gemini to return:

- ``tags`` — short hashtag-style descriptors the user can pin to the
  review (e.g. ``#saffron``, ``#crispy``).
- ``visible_ingredients`` — free-form list of components the model
  recognizes on the plate.
- ``plating_style`` — one of ``minimalist``, ``family-style``,
  ``deconstructed``, ``rustic``, ``classic``.
- ``editorial_blurb`` — 1-2 sentences in CritiComida's editorial tone.
- ``suggested_pros`` / ``suggested_cons`` — short bullets the
  reviewer can accept verbatim or edit.

The model returns a JSON object thanks to ``response_mime_type``. We
validate the shape defensively because vision models occasionally drop
keys or smuggle prose around the JSON.

Failures degrade gracefully: every output field is optional, so the
caller always gets *something* back even if Gemini is misconfigured or
the image is unreachable.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Flash is ~10x cheaper than Pro for this task and the latency is
# noticeable in a "wait for tags" UI flow. Override via env if needed.
_VISION_MODEL = "gemini-2.5-flash"
_REQUEST_TIMEOUT = 25.0
_MAX_TAGS = 8
_MAX_INGREDIENTS = 12

_PLATING_STYLES = {
    "minimalist",
    "family-style",
    "deconstructed",
    "rustic",
    "classic",
}


_SYSTEM_INSTRUCTION = """Sos el Ghostwriter de CritiComida. Mirás fotos de platos y proponés etiquetas + texto editorial corto, en español rioplatense, tono cálido y específico (sin clichés tipo "delicioso").

Devolvé SIEMPRE un JSON válido con esta forma exacta:

{
  "tags": ["palabra1", "palabra2"],          // EXACTAMENTE 3-6 tags, lowercase, sin "#", sin espacios
  "visible_ingredients": ["arroz", "azafrán"], // MÁX 6 ingredientes únicos, lowercase, en español
  "plating_style": "minimalist|family-style|deconstructed|rustic|classic",
  "editorial_blurb": "Frase 1. Frase 2.",    // 1-2 frases, MÁX 200 caracteres total
  "suggested_pros": ["punto 1", "punto 2"],  // MÁX 2 ítems, ≤60 caracteres c/u
  "suggested_cons": ["punto 1"]              // MÁX 2 ítems, ≤60 caracteres c/u
}

Reglas innegociables:
- NO enumeres variantes del mismo ingrediente (un único "caldo", no "caldo de pollo / de carne / de pescado").
- NO repitas conceptos entre tags e ingredientes.
- Si no estás seguro de un campo, devolvé array vacío.
- La respuesta entera tiene que ser JSON válido y compacto.

Si el plato no se puede identificar, devolvé arrays vacíos pero sostené la forma."""


_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "tags": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "visible_ingredients": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "plating_style": {"type": "STRING"},
        "editorial_blurb": {"type": "STRING"},
        "suggested_pros": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "suggested_cons": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
    },
}


class VisionUnavailable(RuntimeError):
    """Raised only when callers explicitly request strict mode."""


async def _fetch_image(url: str) -> tuple[bytes, str]:
    """Download an image and return (bytes, mime). Times out fast: a
    review-creation flow can't wait 30s for a slow CDN."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        return r.content, mime


def _empty_response() -> dict[str, Any]:
    return {
        "tags": [],
        "visible_ingredients": [],
        "plating_style": None,
        "editorial_blurb": None,
        "suggested_pros": [],
        "suggested_cons": [],
    }


def _parse_partial_json(text: str) -> dict[str, Any] | None:
    """Best-effort recovery from truncated JSON output.

    When Gemini hits ``MAX_TOKENS`` mid-array we still want to keep the
    fields it managed to close. We close any open string, drop the
    trailing dangling array element, and balance brackets/braces.
    Returns ``None`` if nothing salvageable.
    """
    import json as _json

    # Try strict first.
    try:
        return _json.loads(text)
    except ValueError:
        pass

    s = text
    # Trim whitespace at the end and a trailing comma if any.
    s = s.rstrip().rstrip(",")

    # Count quote pairs ignoring escaped ones — if odd, there's an open string.
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        # Drop the unterminated trailing string + the comma that opened it.
        last_quote = s.rfind('"')
        # Walk back to the comma or array opener that introduced the bad item.
        cut = s.rfind(",", 0, last_quote)
        if cut < 0:
            cut = s.rfind("[", 0, last_quote)
        if cut < 0:
            return None
        s = s[:cut].rstrip().rstrip(",")

    # Balance brackets/braces.
    open_brace = s.count("{") - s.count("}")
    open_bracket = s.count("[") - s.count("]")
    if open_bracket > 0:
        s += "]" * open_bracket
    if open_brace > 0:
        s += "}" * open_brace

    try:
        return _json.loads(s)
    except ValueError:
        return None


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Clamp and sanitize the model output. The vision model is mostly
    well-behaved but we never trust its bounds."""
    tags = [
        str(t).strip().lower().replace(" ", "-").lstrip("#")
        for t in (raw.get("tags") or [])
        if t and isinstance(t, str)
    ]
    # Dedupe, preserve order, cap.
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
        str(i).strip().lower()
        for i in (raw.get("visible_ingredients") or [])
        if i and isinstance(i, str)
    ][:_MAX_INGREDIENTS]

    plating_style = raw.get("plating_style")
    if isinstance(plating_style, str):
        plating_style = plating_style.strip().lower()
        if plating_style not in _PLATING_STYLES:
            plating_style = None
    else:
        plating_style = None

    blurb = raw.get("editorial_blurb")
    if isinstance(blurb, str):
        blurb = blurb.strip()[:240] or None
    else:
        blurb = None

    def _clip_list(key: str, n: int, char_cap: int) -> list[str]:
        out: list[str] = []
        for x in raw.get(key) or []:
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
        "suggested_pros": _clip_list("suggested_pros", 3, 80),
        "suggested_cons": _clip_list("suggested_cons", 2, 80),
    }


async def analyze_dish_photo(
    *,
    photo_url: str | None = None,
    photo_bytes: bytes | None = None,
    photo_mime: str | None = None,
    dish_hint: str | None = None,
) -> dict[str, Any]:
    """Analyze a dish photo and return Ghostwriter suggestions.

    Pass either ``photo_url`` (we'll fetch it) or ``photo_bytes`` +
    ``photo_mime`` (already in memory, e.g. from a multipart upload).
    ``dish_hint`` lets the caller pass the dish name so the model can
    bias tags toward what it expects (helps disambiguate: "pizza" with
    a hint of "fugazzeta" vs "napolitana" gives different tags).
    """
    key = settings.GEMINI_API_KEY
    if not key:
        return _empty_response()

    if photo_bytes is None and photo_url:
        try:
            photo_bytes, photo_mime = await _fetch_image(photo_url)
        except httpx.HTTPError as exc:
            logger.warning("Couldn't fetch photo %s: %s", photo_url, exc)
            return _empty_response()

    if not photo_bytes:
        return _empty_response()

    import base64

    inline_b64 = base64.b64encode(photo_bytes).decode("ascii")
    text_prompt = "Analizá la foto del plato y devolvé el JSON pedido."
    if dish_hint:
        text_prompt += f" Pista: el plato probablemente es '{dish_hint}'."

    payload: dict[str, Any] = {
        "system_instruction": {
            "parts": [{"text": _SYSTEM_INSTRUCTION}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": photo_mime or "image/jpeg",
                            "data": inline_b64,
                        }
                    },
                    {"text": text_prompt},
                ],
            }
        ],
        "generation_config": {
            "response_mime_type": "application/json",
            "response_schema": _SCHEMA,
            "temperature": 0.4,
            # 800 was tight: a dish with many ingredients + blurb + pros
            # + cons can blow past it and we'd get truncated JSON.
            "max_output_tokens": 2048,
        },
    }
    url = f"{_GEMINI_BASE}/models/{_VISION_MODEL}:generateContent"

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            r = await client.post(url, params={"key": key}, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as exc:
        logger.warning("Gemini vision call failed: %s", exc)
        return _empty_response()

    try:
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason")
        parts = candidate["content"]["parts"]
        # When ``response_mime_type=application/json``, ``text`` is a
        # JSON string; we still parse defensively.
        import json as _json

        first_text = next(
            (p["text"] for p in parts if isinstance(p, dict) and "text" in p),
            None,
        )
        if not first_text:
            logger.warning(
                "Gemini vision returned no text part (finishReason=%s)",
                finish_reason,
            )
            return _empty_response()
        raw = _parse_partial_json(first_text)
        if raw is None:
            logger.warning(
                "Gemini vision returned unparseable payload "
                "(finishReason=%s, len=%d, tail=%r)",
                finish_reason,
                len(first_text),
                first_text[-80:],
            )
            return _empty_response()
        if finish_reason == "MAX_TOKENS":
            logger.info(
                "Gemini vision response was truncated; recovered partial "
                "JSON (len=%d)",
                len(first_text),
            )
    except (KeyError, IndexError) as exc:
        logger.warning("Gemini vision response missing fields: %s", exc)
        return _empty_response()

    return _normalize(raw)
