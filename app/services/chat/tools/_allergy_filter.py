"""Server-side allergy guard for the dish-rendering tools.

The Sommelier prompt has a "filter by allergies" rule (regla 5),
but Gemini Flash Lite ignores it inconsistently: in production the
agent has been observed recommending a dish whose own description
mentions the allergen the comensal just declared. The cost of that
slip is much higher than the cost of being conservative — it can
be a medical issue, and it destroys trust instantly.

This module is the **structural last line of defence**. Tools that
emit dish cards (``recommend_dishes``, ``compare_dishes``) consult
it before serialising the output: any dish whose name, description,
or pros/cons text mentions a declared allergen is dropped. The
caller surfaces the drops to the LLM via ``allergy_drops`` so the
agent can adjust its editorial sentence ("descarté X y Y por tu
restricción de nueces").

Matching is accent-insensitive and case-insensitive substring on
the normalised dish text, but only when the allergen itself is
≥3 chars. Single/two-character allergen strings (likely garbage
from a malformed tool call upstream) are skipped to avoid
catastrophic over-filtering ("a" matching every dish). The
``update_taste_profile`` schema already requires ``minLength: 2``,
but we double-check here.
"""

from __future__ import annotations

import unicodedata
import uuid
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import UserTasteProfile
from app.models.dish import Dish, DishReviewProsConsType


_MIN_ALLERGEN_LENGTH = 3


# Synonyms / multilingual equivalents for the allergens we see most
# in production. Both the user's declared allergen and the dish text
# get normalised through ``_expand_terms`` so a comensal who declared
# "nueces" still matches a dish whose name says "Nuez" (singular), and
# someone allergic to "maní" matches a description mentioning
# "peanut" or "cacahuate". Keys must already be normalised (no accents,
# lowercase). Reverse-lookup is built once at import time.
_ALLERGEN_SYNONYM_GROUPS: list[set[str]] = [
    {"nuez", "nueces", "walnut", "walnuts"},
    {"mani", "manies", "peanut", "peanuts", "cacahuate", "cacahuates"},
    {"leche", "lactosa", "lacteo", "lacteos", "milk", "lactose", "dairy"},
    {"gluten", "trigo", "wheat", "harina", "harinas"},
    {"huevo", "huevos", "egg", "eggs"},
    {"soja", "soya", "soy"},
    {
        "marisco",
        "mariscos",
        "shellfish",
        "camaron",
        "camarones",
        "langostino",
        "langostinos",
        "gamba",
        "gambas",
        "calamar",
        "calamares",
        "pulpo",
        "almeja",
        "almejas",
        "mejillon",
        "mejillones",
    },
    {"pescado", "pescados", "fish"},
    {"miel", "honey"},
    {"sesamo", "sesame", "tahini"},
    {"almendra", "almendras", "almond", "almonds"},
    {"avellana", "avellanas", "hazelnut", "hazelnuts"},
    {"pistacho", "pistachos", "pistachio", "pistachios"},
    {"castana", "castanas", "chestnut", "chestnuts"},
]
_SYNONYM_INDEX: dict[str, set[str]] = {
    term: group for group in _ALLERGEN_SYNONYM_GROUPS for term in group
}


def _stem_variants(term: str) -> set[str]:
    """Generic Spanish-plural stripping fallback for allergens we
    don't have an explicit synonym group for. ``mariscos`` →
    ``{mariscos, marisco}``; ``ananá`` (no plural) returns just
    itself. Length guard avoids stripping into 2-char garbage."""
    out = {term}
    if len(term) > 4 and term.endswith("es"):
        out.add(term[:-2])
    if len(term) > 3 and term.endswith("s"):
        out.add(term[:-1])
    return out


def _expand_terms(term: str) -> set[str]:
    """Given a normalised allergen, return all the surface forms a
    dish text might use to refer to the same thing. Falls back to
    the simple Spanish plural-stripping when there is no explicit
    synonym group."""
    if term in _SYNONYM_INDEX:
        return _SYNONYM_INDEX[term]
    return _stem_variants(term)


def allergen_canonical_key(term: str) -> str:
    """Collapse an allergen string to a stable canonical key so we
    can dedupe synonyms when persisting the profile.

    ``"nueces"`` and ``"nuez"`` both return the same key; same for
    ``"maní"`` / ``"manies"`` / ``"peanut"``. The chosen key is the
    sorted-first member of the synonym group — arbitrary but
    deterministic, and unused beyond comparison. Out-of-index terms
    are normalised + stripped of common Spanish plural endings so
    ``"alcaucil"`` and ``"alcauciles"`` collide too.
    """
    normalised = _normalise(term)
    if not normalised:
        return ""
    if normalised in _SYNONYM_INDEX:
        # ``min`` over the group gives us a deterministic
        # representative regardless of insertion order.
        return min(_SYNONYM_INDEX[normalised])
    if len(normalised) > 4 and normalised.endswith("es"):
        return normalised[:-2]
    if len(normalised) > 3 and normalised.endswith("s"):
        return normalised[:-1]
    return normalised


def _normalise(text: str | None) -> str:
    """Strip diacritics + lowercase + trim. NFD decomposition removes
    combining marks (Mn category) so 'maní' matches 'mani' and 'Maní'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    ).lower().strip()


async def get_user_allergies(
    db: AsyncSession, user_id: uuid.UUID | None
) -> list[str]:
    """Read the comensal's declared allergies. Empty list when no
    auth context or no profile yet."""
    if user_id is None:
        return []
    stmt = select(UserTasteProfile.allergies).where(
        UserTasteProfile.user_id == user_id
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        return []
    return [a for a in row if isinstance(a, str) and len(a.strip()) >= _MIN_ALLERGEN_LENGTH]


def _dish_haystack(dish: Dish) -> str:
    """Concatenate every textual field of the dish a reviewer might
    have mentioned an ingredient in. Reviews + pros/cons aren't
    pulled here (would require a separate join), but the dish
    ``name`` + ``description`` cover the cases we've seen in prod
    (Malabi → "leche con coco, nueces y rosas")."""
    parts: list[str] = [dish.name or ""]
    if dish.description:
        parts.append(dish.description)
    return _normalise(" ".join(parts))


def filter_dishes_by_allergies(
    dishes: Iterable[Dish], allergies: list[str]
) -> tuple[list[Dish], list[dict[str, Any]]]:
    """Split ``dishes`` into (kept, dropped). A dish is dropped if
    its name or description mentions any allergen as a substring on
    the normalised text. Returns a list of drop records the caller
    surfaces so the LLM can explain the decision in its editorial
    response.

    ``allergies`` is expected pre-cleaned by ``get_user_allergies``
    (≥3 chars per entry). If empty, returns the input unchanged.
    """
    if not allergies:
        return list(dishes), []

    normalised_allergens = [
        n
        for n in (_normalise(a) for a in allergies)
        if len(n) >= _MIN_ALLERGEN_LENGTH
    ]
    if not normalised_allergens:
        return list(dishes), []

    # Pre-expand each declared allergen into its synonym set so we
    # match across plural/singular and language variants. Stored as
    # (label, expanded_terms) so the matched-allergen we surface to
    # the LLM is the term the user actually declared, not the stem.
    expanded: list[tuple[str, set[str]]] = []
    for allergen in normalised_allergens:
        terms = {
            _normalise(t)
            for t in _expand_terms(allergen)
        }
        terms = {t for t in terms if len(t) >= _MIN_ALLERGEN_LENGTH}
        if terms:
            expanded.append((allergen, terms))

    kept: list[Dish] = []
    dropped: list[dict[str, Any]] = []
    for dish in dishes:
        haystack = _dish_haystack(dish)
        matched = [
            label
            for label, terms in expanded
            if any(term in haystack for term in terms)
        ]
        if matched:
            dropped.append(
                {
                    "dish_id": str(dish.id),
                    "name": dish.name,
                    "matched_allergens": matched,
                }
            )
        else:
            kept.append(dish)
    return kept, dropped


# Re-export for callers that want to inspect pros_cons too via
# their own loaded ``DishReview`` objects. Kept narrow on purpose
# — pulling reviews here would couple this helper to a specific
# eager-load shape.
__all__ = [
    "DishReviewProsConsType",
    "allergen_canonical_key",
    "filter_dishes_by_allergies",
    "get_user_allergies",
]
