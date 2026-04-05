import os
from typing import Any

import litellm
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import Dish, DishReview, DishReviewProsCons
from app.models.restaurant import Restaurant

# Spanish stop words to filter from keyword extraction
_STOP_WORDS = {
    "de", "la", "el", "en", "que", "es", "un", "una", "del", "los", "las",
    "con", "por", "para", "como", "qué", "cuál", "dónde", "cuáles", "hay",
    "tiene", "tienen", "quiero", "busco", "me", "mi", "tu", "su", "sus",
    "mis", "nos", "más", "muy", "bien", "mal", "bueno", "buena", "mejor",
    "algún", "alguna", "alguno", "algunos", "todas", "todo", "puedo", "puedes",
    "recomiendas", "recomienda", "recomiendan", "comer", "ir", "ver",
    "saber", "sobre", "acerca", "algún", "buen", "gran", "si", "no", "se",
    "al", "le", "lo", "les", "fue", "ser", "han", "son", "era", "tiene",
}

SYSTEM_PROMPT = """Eres el asistente gastronómico de CritiComida, una plataforma de reseñas de restaurantes y platos.

Tu función es responder preguntas sobre restaurantes y platos usando ÚNICAMENTE la información del contexto provisto.

Reglas:
- Responde siempre en español.
- Basa tus respuestas SOLO en el contexto proporcionado.
- Si el contexto no contiene información suficiente para responder, di exactamente: "No tengo información suficiente en mi base de datos para responder esa pregunta."
- Sé conciso y útil. Menciona nombres de restaurantes y platos específicos del contexto.
- No inventes información ni hagas suposiciones fuera del contexto dado.
"""

NO_INFO_RESPONSE = (
    "No tengo información suficiente en mi base de datos para responder esa pregunta. "
    "Puedo ayudarte con preguntas sobre restaurantes y platos registrados en CritiComida."
)


def _extract_keywords(message: str) -> list[str]:
    """Extract meaningful keywords from a user message."""
    words = message.lower().split()
    keywords = [
        w.strip("¿?.,!()\"'")
        for w in words
        if len(w) >= 3 and w.strip("¿?.,!()\"'") not in _STOP_WORDS
    ]
    # Deduplicate while preserving order, take up to 5
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        if kw not in seen and len(kw) >= 3:
            seen.add(kw)
            result.append(kw)
        if len(result) == 5:
            break
    return result


async def _search_restaurants(db: AsyncSession, keywords: list[str]) -> list[Restaurant]:
    """Search restaurants matching any keyword in name or location."""
    if not keywords:
        return []

    conditions = []
    for kw in keywords:
        pattern = f"%{kw}%"
        conditions.append(Restaurant.name.ilike(pattern))
        conditions.append(Restaurant.location_name.ilike(pattern))

    stmt = (
        select(Restaurant)
        .options(
            selectinload(Restaurant.category),
            selectinload(Restaurant.dishes).selectinload(Dish.reviews).selectinload(
                DishReview.pros_cons
            ),
        )
        .where(or_(*conditions))
        .order_by(Restaurant.computed_rating.desc(), Restaurant.review_count.desc())
        .limit(3)
    )
    result = await db.execute(stmt)
    return list(result.scalars().unique().all())


def _build_context(restaurants: list[Restaurant]) -> str:
    """Build a structured context string from restaurant data."""
    if not restaurants:
        return ""

    parts: list[str] = []
    for r in restaurants:
        category_name = r.category.name if r.category else "Sin categoría"
        rating_str = f"{float(r.computed_rating):.1f}/5"
        lines = [
            f"=== RESTAURANTE: {r.name} ===",
            f"Ubicación: {r.location_name}",
            f"Categoría: {category_name} | Rating: {rating_str} ({r.review_count} reseñas)",
        ]
        if r.description:
            lines.append(f"Descripción: {r.description}")

        # Top 3 dishes by rating
        top_dishes = sorted(
            r.dishes, key=lambda d: (float(d.computed_rating), d.review_count), reverse=True
        )[:3]
        if top_dishes:
            lines.append("Platos destacados:")
            for dish in top_dishes:
                dish_rating = f"★{float(dish.computed_rating):.1f}"
                dish_line = f"  - {dish.name} ({dish_rating}, {dish.review_count} reseñas)"
                if dish.description:
                    dish_line += f": {dish.description}"

                # Top 2 reviews
                sorted_reviews = sorted(
                    dish.reviews, key=lambda rv: rv.rating, reverse=True
                )[:2]
                for rv in sorted_reviews:
                    if rv.note:
                        pros = [pc.text for pc in rv.pros_cons if pc.type.value == "pro"][:2]
                        cons = [pc.text for pc in rv.pros_cons if pc.type.value == "con"][:2]
                        note_line = f'    "{rv.note[:150]}"'
                        if pros:
                            note_line += f" | Pros: {', '.join(pros)}"
                        if cons:
                            note_line += f" | Contras: {', '.join(cons)}"
                        dish_line += f"\n{note_line}"

                lines.append(dish_line)

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


async def get_chat_response(
    db: AsyncSession,
    message: str,
    history: list[dict[str, Any]],
) -> str:
    """Main entry point: retrieve context from DB and call LLM."""
    keywords = _extract_keywords(message)
    restaurants = await _search_restaurants(db, keywords)
    context = _build_context(restaurants)

    if not context:
        return NO_INFO_RESPONSE

    model = os.getenv("CHAT_MODEL", "anthropic/claude-haiku-4-5-20251001")
    api_key = os.getenv("CHAT_API_KEY") or None

    user_content = f"Contexto de la base de datos CritiComida:\n{context}\n\nPregunta del usuario: {message}"

    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Include recent history (last 6 messages) for conversational context
    for turn in history[-6:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_content})

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 512,
    }
    if api_key:
        kwargs["api_key"] = api_key

    response = await litellm.acompletion(**kwargs)
    return response.choices[0].message.content or NO_INFO_RESPONSE
