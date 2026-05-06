"""Discovery tools beyond plain ``search_dishes``.

``surprise_me`` is a serendipity primitive for the Sommelier: the
comensal asks for "something different" / "sorprendeme" / "no sé qué
quiero" and the tool returns ONE dish chosen specifically *outside*
their reviewed history (a category or neighborhood they don't
frequent), while still respecting declared allergies. The agent
cites the returned ``serendipity_reason`` in its editorial sentence.

Why deterministic-per-day randomness: a "sorprendeme" repeated three
times in the same session shouldn't rotate three different dishes —
the comensal would feel the bot is just throwing options at random.
Seeding with ``user_id + date`` keeps the suggestion stable until
tomorrow, which reads as "this is what I picked for you today."

The tool is **data-only** (``emits_card=False``). After receiving the
suggestion, the agent decides whether to present it as a card via
``recommend_dishes(dish_ids=[id])``. Splitting discovery from
presentation is the same contract that ``search_dishes`` follows
(see ``tools/recommend.py`` docstring for the rationale).
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.category import Category
from app.models.dish import Dish, DishReview, DishReviewProsConsType
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._resolution import _resolve_dish_global
from app.services.chat.tools._schemas import (
    CompareDishesInput,
    SurpriseMeInput,
    pydantic_to_anthropic_schema,
)
from app.services.chat.tools._wishlist_lookup import get_saved_dish_ids
from app.services.taste_profile_service import get_taste_profile


# ──────────────────────────────────────────────────────────────────────────
#   Allergy → category-slug blocklist
# ──────────────────────────────────────────────────────────────────────────


# Conservative mapping: when in doubt we EXCLUDE the category. A
# "pizza sin gluten" exists, but the conservative choice for a
# celíaco's surprise pick is to skip the whole italiana / burguers
# bucket rather than recommend a dish whose default is gluten-heavy.
# False positives ("we missed offering you the gluten-free pizza")
# are recoverable; false negatives ("we recommended you a pizza
# despite gluten") are not.
_ALLERGY_CATEGORY_BLOCKLIST: dict[str, set[str]] = {
    # Wheat / gluten-containing.
    "gluten": {
        "italiana",
        "burguers",
        "mexico-food",
        "thaifood",
        "chinafood",
        "brunchs",
    },
    "trigo": {
        "italiana",
        "burguers",
        "mexico-food",
        "thaifood",
        "chinafood",
    },
    "wheat": {
        "italiana",
        "burguers",
        "mexico-food",
        "thaifood",
        "chinafood",
    },
    # Dairy. Helados/dulces are dessert-leaning; conservatively skip.
    "lácteo": {"helados", "dulces"},
    "lacteo": {"helados", "dulces"},
    "lácteos": {"helados", "dulces"},
    "lactose": {"helados", "dulces"},
    "leche": {"helados", "dulces"},
    "dairy": {"helados", "dulces"},
    "milk": {"helados", "dulces"},
}


def _blocked_categories_for(allergies: list[str]) -> set[str]:
    """Build the category-slug exclusion set for the comensal's
    declared allergies. Match is case-insensitive substring on each
    declared allergy string against the blocklist keys."""
    blocked: set[str] = set()
    for raw in allergies:
        lowered = raw.lower().strip()
        for token, cats in _ALLERGY_CATEGORY_BLOCKLIST.items():
            if token in lowered:
                blocked.update(cats)
    return blocked


# ──────────────────────────────────────────────────────────────────────────
#   surprise_me
# ──────────────────────────────────────────────────────────────────────────


_CANDIDATE_POOL = 20
"""How many top-rated candidates to pull before the novelty filter.
Twenty rows is enough to give the seeded RNG something to choose
from even after we drop overlap with the comensal's history; small
enough to keep the query cheap."""


def make_surprise_me_tool(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
) -> ToolSpec:
    """Build the ``surprise_me`` tool bound to the authenticated
    comensal (or ``None`` for anonymous use, which falls back to a
    pure top-rated random pick)."""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            inputs = SurpriseMeInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for surprise_me.",
                "details": exc.errors(include_url=False),
            }

        # Pull the comensal's profile for novelty-aware filtering. An
        # anonymous user gets a generic top-rated random pick — still
        # useful, just less personalised.
        profile = (
            await get_taste_profile(db, user_id) if user_id is not None else None
        )
        top_categories = (
            {c.lower() for c in (profile.top_categories or [])} if profile else set()
        )
        top_neighborhoods = (
            {n.lower() for n in (profile.top_neighborhoods or [])} if profile else set()
        )
        allergies = list(profile.allergies or []) if profile else []
        blocked_categories = _blocked_categories_for(allergies)

        stmt = (
            select(Dish)
            .join(Restaurant, Dish.restaurant_id == Restaurant.id)
            .options(
                selectinload(Dish.restaurant).selectinload(Restaurant.category),
            )
            .where(
                and_(
                    Dish.computed_rating >= 4.0,
                    Dish.review_count > 0,
                )
            )
        )

        if inputs.neighborhood:
            stmt = stmt.where(
                Restaurant.location_name.ilike(f"%{inputs.neighborhood}%")
            )

        if blocked_categories:
            stmt = stmt.join(Category, Restaurant.category_id == Category.id).where(
                Category.slug.notin_(blocked_categories)
            )

        stmt = stmt.order_by(Dish.computed_rating.desc()).limit(_CANDIDATE_POOL)
        candidates = list((await db.execute(stmt)).scalars().all())

        if not candidates:
            return {
                "error": "no_match",
                "message": (
                    "No encontré candidatos para sorprender al comensal. "
                    "Puede que el barrio o las alergias dejen el pool "
                    "vacío — proponé buscar en otra zona o sin filtros."
                ),
            }

        # Novelty filter: prefer dishes whose category OR neighborhood
        # falls outside the comensal's reviewed history. Fall back to
        # the full pool if novelty leaves nothing — better to suggest
        # a dish from their familiar territory than refuse to answer.
        novel: list[Dish] = []
        for d in candidates:
            rest = d.restaurant
            if rest is None:
                continue
            cat_slug = (
                rest.category.slug.lower()
                if rest.category and rest.category.slug
                else None
            )
            loc = (rest.location_name or "").lower()
            in_top_cat = cat_slug in top_categories if cat_slug else False
            in_top_nbhd = any(n in loc for n in top_neighborhoods)
            if not in_top_cat or not in_top_nbhd:
                novel.append(d)

        pool = novel if novel else candidates

        # Deterministic per (user, day, neighborhood) so a "sorprendeme"
        # repeated in the same session lands on the same dish; tomorrow
        # the seed flips and the bot has a fresh pick.
        seed_input = (
            f"{user_id or 'anon'}-{date.today().isoformat()}-"
            f"{inputs.neighborhood or ''}"
        )
        rng = random.Random(seed_input)
        chosen = rng.choice(pool)

        # Compose the editorial reason the agent will quote verbatim.
        rest = chosen.restaurant
        cat_slug = (
            rest.category.slug.lower()
            if rest is not None and rest.category and rest.category.slug
            else None
        )
        loc = (rest.location_name if rest is not None else "") or ""
        loc_lower = loc.lower()
        reasons: list[str] = []
        if cat_slug and cat_slug not in top_categories:
            reasons.append("una categoría que no frecuentás")
        if not any(n in loc_lower for n in top_neighborhoods):
            reasons.append("un barrio donde no reseñás seguido")
        if not reasons:
            # Fallback: dish came from familiar territory but is still
            # a high-rated pick the comensal hasn't reviewed.
            reasons.append("un plato bien rankeado que vale la pena explorar")
        serendipity_reason = " y ".join(reasons)

        return {
            "dish_id": str(chosen.id),
            "name": chosen.name,
            "rating": (
                float(chosen.computed_rating)
                if chosen.computed_rating is not None
                else None
            ),
            "restaurant_name": rest.name if rest is not None else None,
            "restaurant_slug": rest.slug if rest is not None else None,
            "location_name": loc or None,
            "category_slug": cat_slug,
            "serendipity_reason": serendipity_reason,
            "respected_allergies": allergies,
        }

    return ToolSpec(
        name="surprise_me",
        description=(
            "Pick ONE high-rated dish that's OUTSIDE the comensal's "
            "reviewed history (category or neighborhood they don't "
            "frequent), respecting any declared allergies. Use when "
            "the comensal asks 'sorprendeme', 'algo distinto', "
            "'no sé qué quiero'. Returns the dish + a "
            "``serendipity_reason`` you should quote in your editorial "
            "sentence (\"te traigo un X porque es {serendipity_reason}\"). "
            "**Data-only** — to actually show the dish as a card, "
            "follow up with `recommend_dishes(dish_ids=[returned_id])`. "
            "Selection is stable per (user, day) so 'sorprendeme' "
            "repeated in the same session lands on the same plato."
        ),
        input_schema=pydantic_to_anthropic_schema(SurpriseMeInput),
        handler=handler,
        emits_card=False,
    )


# ──────────────────────────────────────────────────────────────────────────
#   compare_dishes — side-by-side comparison grid
# ──────────────────────────────────────────────────────────────────────────


_COMPARE_TOP_REVIEWS = 5
"""How many recent reviews to inspect per dish for the pros/cons
aggregation. Five gives enough volume to average pillars meaningfully
without inflating query cost when comparing 4 dishes at once."""


async def _build_comparison_entry(
    db: AsyncSession,
    dish: Dish,
    *,
    saved_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Build the per-dish payload for ``compare_dishes``.

    Pulls the dish's top-N most recent reviews (with pros/cons) and
    folds them into:

    - ``pillar_breakdown``: per-pillar average across reviews.
    - ``top_pros`` / ``top_cons``: the two most-mentioned pros/cons,
      counted across the inspected reviews. We use frequency rather
      than recency because a "salty" complaint that shows up four
      times across reviews is more meaningful for a comparison than
      a one-off mention.
    - ``want_to_try``: whether the comensal already saved this dish;
      lets the FE paint the bookmark chip correctly on first render
      even after a page refresh. ``False`` when no auth context.
    """
    review_stmt = (
        select(DishReview)
        .where(DishReview.dish_id == dish.id)
        .options(selectinload(DishReview.pros_cons))
        .order_by(DishReview.created_at.desc())
        .limit(_COMPARE_TOP_REVIEWS)
    )
    reviews = list((await db.execute(review_stmt)).scalars().all())

    pillar_avg: dict[str, float | None] = {}
    for pillar in ("presentation", "execution", "value_prop"):
        values = [
            getattr(r, pillar)
            for r in reviews
            if getattr(r, pillar) is not None
        ]
        pillar_avg[pillar] = (
            round(sum(values) / len(values), 2) if values else None
        )

    pros_counter: Counter[str] = Counter()
    cons_counter: Counter[str] = Counter()
    for r in reviews:
        for pc in r.pros_cons or []:
            text = (pc.text or "").strip()
            if not text:
                continue
            if pc.type is DishReviewProsConsType.pro:
                pros_counter[text] += 1
            elif pc.type is DishReviewProsConsType.con:
                cons_counter[text] += 1

    rest = dish.restaurant
    return {
        "dish_id": str(dish.id),
        "name": dish.name,
        "cover_image_url": dish.cover_image_url,
        "rating": (
            float(dish.computed_rating)
            if dish.computed_rating is not None
            else None
        ),
        "review_count": dish.review_count,
        "price_tier": (
            dish.price_tier.value if dish.price_tier is not None else None
        ),
        "restaurant_name": rest.name if rest is not None else None,
        "restaurant_slug": rest.slug if rest is not None else None,
        "location_name": (
            rest.location_name if rest is not None else None
        ),
        "lat": (
            float(rest.latitude)
            if rest is not None and rest.latitude is not None
            else None
        ),
        "lng": (
            float(rest.longitude)
            if rest is not None and rest.longitude is not None
            else None
        ),
        "pillar_breakdown": pillar_avg,
        "top_pros": [text for text, _ in pros_counter.most_common(2)],
        "top_cons": [text for text, _ in cons_counter.most_common(2)],
        "want_to_try": (saved_ids is not None and dish.id in saved_ids),
    }


def make_compare_dishes_tool(
    db: AsyncSession, *, user_id: uuid.UUID | None = None
) -> ToolSpec:
    """Build the ``compare_dishes`` tool.

    Side-by-side comparative view of 2-4 dishes. Accepts uuids
    (``dish_ids``) or free-form names (``dish_names``); names are
    resolved with ``_resolve_dish_global`` so the agent never has to
    ask the human for a uuid. If any one name is ambiguous (multiple
    matches) or missing, the tool returns the first guidance payload
    encountered so the agent can disambiguate before retrying.

    Emits a card — the visible grid is exactly the dishes the agent
    asked about. The shape differs from ``recommend_dishes``: each
    entry carries a ``pillar_breakdown`` and aggregated pros/cons so
    the FE can render the comparison columns.
    """

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            inputs = CompareDishesInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for compare_dishes.",
                "details": exc.errors(include_url=False),
            }

        ids = inputs.dish_ids or []
        names = inputs.dish_names or []
        if not ids and not names:
            return {
                "error": "missing_input",
                "message": (
                    "Pasame al menos 2 platos a comparar — como "
                    "``dish_ids`` (uuids de search_dishes) o "
                    "``dish_names`` (texto libre). Mínimo 2, máximo 4."
                ),
            }

        # Pair each input slot with whichever channel was used. Order
        # matters — the FE renders columns in the order received.
        pairs: list[tuple[str | None, str | None]] = []
        if ids and names:
            # Both provided: align by index, padding with None where
            # one list is shorter.
            for i in range(max(len(ids), len(names))):
                pairs.append(
                    (
                        ids[i] if i < len(ids) else None,
                        names[i] if i < len(names) else None,
                    )
                )
        elif ids:
            pairs = [(d, None) for d in ids]
        else:
            pairs = [(None, n) for n in names]

        if len(pairs) < 2:
            return {
                "error": "too_few",
                "message": (
                    "Necesito al menos 2 platos para comparar — "
                    "comparar uno solo no es comparar."
                ),
            }
        if len(pairs) > 4:
            pairs = pairs[:4]

        resolved: list[Dish] = []
        dropped: list[dict[str, Any]] = []
        for dish_id, dish_name in pairs:
            dish, err = await _resolve_dish_global(
                db,
                restaurant_scope_id=None,
                dish_id=dish_id,
                dish_name=dish_name,
                actor="comensal",
            )
            if err is not None:
                # If any single slot is ambiguous we stop and surface
                # the resolver's payload — that's the LLM's cue to
                # ask the comensal to disambiguate before we render
                # a half-built grid. Stash what was already resolved
                # so the agent has context for the disambiguation
                # prompt.
                err["resolved_so_far"] = [
                    {"dish_id": str(d.id), "name": d.name} for d in resolved
                ]
                err["unresolved_slot"] = {
                    "dish_id": dish_id,
                    "dish_name": dish_name,
                }
                return err
            assert dish is not None
            resolved.append(dish)

        if len(resolved) < 2:
            return {
                "error": "too_few_resolved",
                "message": (
                    "Después de resolver los nombres me quedan menos "
                    "de 2 platos. Pedile al comensal que confirme "
                    "los platos a comparar."
                ),
                "dropped": dropped,
            }

        # Bulk lookup of bookmark state for the resolved dishes —
        # one query, even when comparing 4. The FE needs this to
        # paint the wishlist chip correctly on first render.
        saved_ids = await get_saved_dish_ids(
            db,
            user_id=user_id,
            dish_ids=[d.id for d in resolved],
        )
        entries = [
            await _build_comparison_entry(db, dish, saved_ids=saved_ids)
            for dish in resolved
        ]
        return {
            "comparison": True,
            "count": len(entries),
            "dishes": entries,
        }

    return ToolSpec(
        name="compare_dishes",
        description=(
            "Side-by-side comparison of 2-4 dishes. Accepts ``dish_ids`` "
            "(uuids from a previous search_dishes) OR ``dish_names`` "
            "(free text the comensal used like 'el risotto', 'la pizza'). "
            "Returns each dish with rating, review_count, price_tier, "
            "restaurant info, ``pillar_breakdown`` (avg presentation/"
            "execution/value_prop across recent reviews) and aggregated "
            "``top_pros``/``top_cons``. Emits a card grid (ComparisonCard) "
            "in the order you passed the inputs — first column is the "
            "primary contender. Use when the comensal asks '¿cuál es "
            "mejor X o Y?', 'compará A vs B', 'qué me conviene'. NEVER "
            "ask the human for a uuid."
        ),
        input_schema=pydantic_to_anthropic_schema(CompareDishesInput),
        handler=handler,
        emits_card=True,
    )
