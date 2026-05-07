"""Vision-backed chat tools.

Two tools live here, both wrappers around ``vision_service.analyze_dish_photo``:

- ``suggest_tags_from_photo`` (Ghostwriter): the editorial helper.
  Returns tags + blurb + pros/cons so the user can pin them on a draft
  review without opening the formal form.
- ``identify_dish_from_photo`` (Sommelier): the discovery helper. Uses
  the same vision call but pipes its output (tags + ingredients) into
  ``search_dishes`` as a ``semantic_query`` so the comensal sees catalog
  matches for the photo they shared.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.chat.agent_loop import ToolSpec
from app.services.vision_service import analyze_dish_photo


SUGGEST_TAGS_FROM_PHOTO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "photo_url": {
            "type": "string",
            "description": "Public URL of the dish photo to analyze.",
        },
        "dish_hint": {
            "type": "string",
            "description": (
                "Optional dish name to bias the model — e.g. 'risotto' "
                "helps the bot pick saffron over generic 'rice' tags."
            ),
        },
    },
    "required": ["photo_url"],
    "additionalProperties": False,
}


def make_suggest_tags_from_photo_tool() -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = await analyze_dish_photo(
            photo_url=args["photo_url"],
            dish_hint=args.get("dish_hint"),
        )
        return {
            "tags": result["tags"],
            "visible_ingredients": result["visible_ingredients"],
            "plating_style": result["plating_style"],
            "editorial_blurb": result["editorial_blurb"],
            "suggested_pros": result["suggested_pros"],
            "suggested_cons": result["suggested_cons"],
        }

    return ToolSpec(
        name="suggest_tags_from_photo",
        description=(
            "Analyze a dish photo and return suggested tags, ingredients, "
            "plating style, an editorial blurb, and pros/cons. Use this "
            "when the user shares a photo and wants help describing or "
            "tagging the dish."
        ),
        input_schema=SUGGEST_TAGS_FROM_PHOTO_SCHEMA,
        handler=handler,
        # Vision calls are heavier than DB lookups: give them more headroom.
        timeout_seconds=30.0,
        emits_card=True,
    )


# ──────────────────────────────────────────────────────────────────────────
#   identify_dish_from_photo (Sommelier)
# ──────────────────────────────────────────────────────────────────────────


IDENTIFY_DISH_FROM_PHOTO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "photo_url": {
            "type": "string",
            "description": (
                "URL of the dish photo. Pass exactly the URL that arrived "
                "in the [foto: <url>] prefix of the user's message — local "
                "/uploads/<filename> paths are read from disk server-side; "
                "absolute http(s) URLs are fetched."
            ),
        },
        "dish_hint": {
            "type": "string",
            "description": (
                "Optional free-text hint from the user's message after the "
                "photo prefix (e.g. 'qué es esto', 'es ramen?', 'lo de "
                "Eretz?'). Biases the vision tagger toward what the comensal "
                "thinks they see, useful for visually similar dishes."
            ),
        },
        "neighborhood": {
            "type": "string",
            "description": (
                "Optional location_name substring used to scope the catalog "
                "match (e.g. 'Palermo'). Pass it when the same message "
                "mentions a barrio."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 8,
            "description": "Max matches to return. Defaults to 6.",
        },
    },
    "required": ["photo_url"],
    "additionalProperties": False,
}


def make_identify_dish_from_photo_tool(
    db: AsyncSession,
    *,
    embed_query: Any | None = None,
    user_id: uuid.UUID | None = None,
) -> ToolSpec:
    """Build the ``identify_dish_from_photo`` tool for the Sommelier.

    Pipeline:

    1. Resolve the photo URL to bytes + mime (local ``/uploads/`` reads
       from disk; absolute URLs are fetched via httpx).
    2. Run two Gemini calls in parallel via ``asyncio.gather``:

       - ``analyze_dish_photo`` (Gemini 2.5 Flash) → tags + visible
         ingredients + plating, used for the agent's editorial reply.
       - ``embed_image`` (Gemini Embedding 2, multimodal) → 768-dim
         vector in the **same space** as ``dish_embeddings``.

    3. Run ``execute_dish_search`` with the image vector as
       ``query_vector``. Cosine distance against text-derived dish
       embeddings is meaningful because Gemini Embedding 2 maps text
       and images into a unified semantic space.

    4. Resilience: if the image embed fails (rare — usually network
       hiccup or HEIC quirks) but vision succeeded, fall back to
       text-embedding the joined tags+ingredients via ``embed_query``.
       That preserves discovery when only one of the two Gemini calls
       is degraded; before this fallback the tool would just say
       "vision unavailable" even though we had usable signal.

    Stays **data-only** — same contract as ``search_dishes``. The
    agent reads ``matches`` and chains ``recommend_dishes(dish_ids=
    [..])`` to surface cards.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        import asyncio
        import os

        from app.routers.images import UPLOAD_DIR
        from app.services.chat.tools._schemas import SearchDishesInput
        from app.services.chat.tools.search import execute_dish_search
        from app.services.embeddings_service import embed_image

        photo_url = (args.get("photo_url") or "").strip()
        if not photo_url:
            return {
                "error": "missing_photo_url",
                "matches": [],
                "count": 0,
                "message": (
                    "No llegó URL de foto. Confirmá que el comensal "
                    "adjuntó una imagen y reintentá."
                ),
            }

        dish_hint = args.get("dish_hint")
        neighborhood = args.get("neighborhood")
        try:
            limit = max(1, min(int(args.get("limit") or 6), 8))
        except (TypeError, ValueError):
            limit = 6

        # ── 1. Resolve photo URL → bytes ───────────────────────────
        # Both vision and embed_image accept inline bytes, so we
        # always materialize the photo locally. For ``/uploads/...``
        # we read from disk (avoiding HTTP loopback into our own
        # backend, which would fail anyway because the URL has no
        # host); absolute URLs are fetched once and shared.
        photo_bytes: bytes | None = None
        photo_mime: str | None = None
        if photo_url.startswith("/uploads/"):
            filename = os.path.basename(photo_url)
            filepath = os.path.join(UPLOAD_DIR, filename)
            if not os.path.exists(filepath):
                return {
                    "error": "photo_not_found",
                    "matches": [],
                    "count": 0,
                    "message": (
                        "El archivo de foto no existe en el servidor. "
                        "Pedile al comensal que la suba de nuevo."
                    ),
                }
            with open(filepath, "rb") as f:
                photo_bytes = f.read()
            ext = os.path.splitext(filename)[1].lower()
            photo_mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".heic": "image/heic",
            }.get(ext, "image/jpeg")
        else:
            # Absolute URL — fetch once with a short timeout so a
            # slow CDN does not hold the whole tool call hostage.
            import httpx as _httpx

            try:
                async with _httpx.AsyncClient(
                    timeout=10.0, follow_redirects=True
                ) as client:
                    r = await client.get(photo_url)
                    r.raise_for_status()
                    photo_bytes = r.content
                    photo_mime = (
                        r.headers.get("content-type", "image/jpeg")
                        .split(";")[0]
                        .strip()
                    )
            except _httpx.HTTPError:
                return {
                    "error": "photo_fetch_failed",
                    "matches": [],
                    "count": 0,
                    "message": (
                        "No pude bajar la foto desde la URL provista. "
                        "Pedile al comensal que la suba de nuevo."
                    ),
                }

        # ── 2. Vision + image embed en paralelo ─────────────────────
        # gemini-embedding-2 mapea imágenes y texto al MISMO espacio
        # 768-dim, así que el vector resultante se compara directo
        # contra ``dish_embeddings`` (texto-derivados) por cosine
        # distance. Esto reemplaza la cadena previa
        # vision→concatenar_tags→text-embed→KNN, que perdía señal en
        # el paso de tags.
        vision_task = analyze_dish_photo(
            photo_bytes=photo_bytes,
            photo_mime=photo_mime,
            dish_hint=dish_hint,
        )
        embed_task = embed_image(photo_bytes, mime_type=photo_mime or "image/jpeg")
        vision, image_vector = await asyncio.gather(
            vision_task, embed_task, return_exceptions=False
        )

        tags = vision.get("tags") or []
        ingredients = vision.get("visible_ingredients") or []
        plating = vision.get("plating_style")

        # ── 3. Decidir el query_vector ──────────────────────────────
        # Preferimos el image vector (sin pérdida); si falla, caemos
        # a text-embedding de los tags como red de seguridad. Sin
        # signals + sin vector → no_signal honesto.
        query_vector: list[float] | None = image_vector
        matched_via = "multimodal_image_embedding"

        if query_vector is None and (tags or ingredients) and embed_query is not None:
            signals: list[str] = []
            seen: set[str] = set()
            for word in (*tags, *ingredients):
                w = (word or "").strip().lower()
                if not w or w in seen:
                    continue
                seen.add(w)
                signals.append(w)
                if len(signals) >= 8:
                    break
            if signals:
                try:
                    query_vector = await embed_query(" ".join(signals))
                    if query_vector is not None:
                        matched_via = "vision_tags_text_embedding"
                except Exception:
                    query_vector = None

        if query_vector is None:
            # Ni multimodal embed ni text-embed fallback funcionaron.
            empty: dict[str, Any] = {
                "matches": [],
                "count": 0,
                "detected": {
                    "tags": tags,
                    "visible_ingredients": ingredients,
                    "plating_style": plating,
                },
                "no_signal": not (tags or ingredients),
            }
            if not settings.GEMINI_API_KEY:
                empty["vision_unavailable"] = True
            return empty

        # ── 4. KNN search con vector pre-computado ──────────────────
        search_inputs = SearchDishesInput.model_validate(
            {
                "neighborhood": neighborhood,
                "limit": limit,
            }
        )
        search_result = await execute_dish_search(
            db,
            inputs=search_inputs,
            restaurant_scope_id=None,
            user_id=user_id,
            query_vector=query_vector,
        )

        payload: dict[str, Any] = {
            "matches": search_result.get("dishes", []),
            "count": search_result.get("count", 0),
            "detected": {
                "tags": tags,
                "visible_ingredients": ingredients,
                "plating_style": plating,
            },
            # ``matched_via`` le dice al agente cómo se logró el match
            # — útil para citarlo si suma editorialmente y para
            # debugging. ``multimodal_image_embedding`` es el path
            # principal; ``vision_tags_text_embedding`` indica
            # degradación silenciosa.
            "matched_via": matched_via,
        }
        # Pass-through allergy metadata para que el agente respete las
        # mismas reglas que en search_dishes.
        for key in ("allergy_drops", "respected_allergies", "safe_subset_note"):
            if key in search_result:
                payload[key] = search_result[key]
        return payload

    return ToolSpec(
        name="identify_dish_from_photo",
        description=(
            "Identify a dish in a photo against the CritiComida catalog. "
            "Call this the moment a user message arrives with a "
            "[foto: <url>] prefix — that prefix is the FE convention for "
            "an attached image. Internally: (1) Gemini 2.5 Flash extracts "
            "tags + visible ingredients (used by you for the editorial "
            "reply, not for matching), (2) Gemini Embedding 2 embeds the "
            "photo directly into the same 768-dim space as "
            "``dish_embeddings`` and KNN-matches against the catalog. "
            "Both calls run in parallel. Returns ``matches`` (same "
            "per-dish shape as search_dishes' dishes), ``detected`` "
            "(tags/visible_ingredients/plating_style for editorial "
            "narration), ``matched_via`` ('multimodal_image_embedding' "
            "primary path, 'vision_tags_text_embedding' degraded "
            "fallback), and the same allergy metadata search_dishes "
            "surfaces. **Data-only**: the comensal does NOT see cards "
            "from this tool. After reading matches, chain "
            "``recommend_dishes(dish_ids=[..])`` with the 1-3 best "
            "matches. ``no_signal: true`` + empty matches means the "
            "photo wasn't interpretable; say so and ask for a "
            "description in words. ``vision_unavailable: true`` means "
            "Gemini is down — degrade gracefully. NEVER answer 'no puedo "
            "ver imágenes': this tool IS your eyes."
        ),
        input_schema=IDENTIFY_DISH_FROM_PHOTO_SCHEMA,
        handler=handler,
        # Vision (~10-25s) y embed_image (~1-3s) corren en paralelo via
        # asyncio.gather; el wall time es el max de ambos. KNN search
        # es <100ms. Mantenemos 35s para margen de redes mobile.
        timeout_seconds=35.0,
        emits_card=False,
    )
