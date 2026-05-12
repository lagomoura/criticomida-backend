"""``recommend_dishes`` — curated grid for the Sommelier.

Why a separate tool instead of letting ``search_dishes`` emit cards?

``search_dishes`` mixes two responsibilities: (1) discovery — give the
agent rows it can read — and (2) presentation — paint cards visible to
the comensal. When both happen in the same call, the comensal sees
*everything the search returned*, including the long tail (Açai, IPA,
Burritos when they asked for café). The agent has no control over the
visible grid.

Splitting the responsibilities lets the agent curate:

1. Call ``search_dishes(...)`` to look the catalog over (no cards).
2. Read the rows and decide which 1-6 actually answer the question.
3. Call ``recommend_dishes(dish_ids=[...])`` to present those.

The visible grid is now exactly the agent's recommendation. The text
("te recomiendo Café Turco") and the cards stay in sync structurally.

Validation: every passed UUID must exist; missing/unparseable ids are
dropped silently and reported in ``dropped_ids`` so the agent can
adjust on the next iteration. If the result would be empty after
filtering, we surface ``error: "no_valid_ids"`` so the agent doesn't
emit an empty grid.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dish import Dish
from app.models.restaurant import Restaurant
from app.services.chat.agent_loop import ToolSpec
from app.services.chat.tools._allergy_filter import (
    filter_dishes_by_allergies,
    get_user_allergies,
)
from app.services.chat.tools._schemas import (
    RecommendDishesInput,
    pydantic_to_anthropic_schema,
)
from app.services.chat.tools._wishlist_lookup import get_saved_dish_ids
from app.services.chat.tools.search import _serialize_dish
from app.services.sommelier_recall_service import (
    enqueue_sommelier_review_recalls,
)


logger = logging.getLogger(__name__)


def make_recommend_dishes_tool(
    db: AsyncSession, *, user_id: uuid.UUID | None = None
) -> ToolSpec:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            inputs = RecommendDishesInput.model_validate(args)
        except ValidationError as exc:
            return {
                "error": "Invalid arguments for recommend_dishes.",
                "details": exc.errors(include_url=False),
            }

        # Parse + dedupe UUIDs while preserving the order the agent
        # passed (agent may rank by relevance — first id is "best").
        seen: set[uuid.UUID] = set()
        ordered_uids: list[uuid.UUID] = []
        bad_ids: list[str] = []
        for raw in inputs.dish_ids:
            try:
                uid = uuid.UUID(raw)
            except (ValueError, TypeError):
                bad_ids.append(raw)
                continue
            if uid in seen:
                continue
            seen.add(uid)
            ordered_uids.append(uid)

        if not ordered_uids:
            return {
                "error": "no_valid_ids",
                "message": (
                    "Los dish_ids no parsearon como UUID. Llamá "
                    "search_dishes primero y pasá los uuids del output."
                ),
                "dropped_ids": bad_ids,
            }

        # Pull all the dishes in one query — keep the result mapped by
        # id so we can preserve the agent's order.
        rows = list(
            (
                await db.execute(
                    select(Dish)
                    .where(Dish.id.in_(ordered_uids))
                    .options(
                        selectinload(Dish.restaurant).selectinload(
                            Restaurant.category
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_id = {d.id: d for d in rows}

        ordered_dishes: list[Dish] = []
        missing_ids: list[str] = []
        for uid in ordered_uids:
            dish = by_id.get(uid)
            if dish is None:
                missing_ids.append(str(uid))
            else:
                ordered_dishes.append(dish)

        if not ordered_dishes:
            return {
                "error": "no_match",
                "message": (
                    "Ninguno de los dish_ids pasados existe en la base. "
                    "Asegurate de copiar los uuids del search_dishes "
                    "exactos, sin manipularlos."
                ),
                "dropped_ids": bad_ids,
                "missing_ids": missing_ids,
            }

        # Server-side allergy guard. The prompt tells the agent to
        # filter mentally, but Flash Lite slips — in production the
        # agent recommended a postre with nueces to a comensal who
        # had just declared a nut allergy. We drop dishes whose name
        # or description mentions any declared allergen and surface
        # the drops to the LLM via ``allergy_drops`` so the next
        # iteration can frame the answer correctly. ``no_safe_dishes``
        # is set when the entire batch falls — agent should NOT emit
        # an empty grid; better to say "ningún plato pasó tu
        # restricción" and propose another search.
        allergies = await get_user_allergies(db, user_id=user_id)
        kept_dishes, dropped = filter_dishes_by_allergies(
            ordered_dishes, allergies
        )
        if not kept_dishes and dropped:
            return {
                "error": "no_safe_dishes",
                "allergy_drops": dropped,
                "respected_allergies": allergies,
                "message": (
                    "Todos los dishes que pediste recomendar mencionan "
                    "un ingrediente al que el comensal declaró ser "
                    "alérgico. NO emitas la grilla; decílo en texto y "
                    "proponé buscar en otra cocina o categoría. NUNCA "
                    "recomiendes un dish cuya descripción menciona el "
                    "alérgeno declarado."
                ),
            }

        # Look up the comensal's wishlist state per dish so the FE
        # paints the bookmark chip correctly even after a refresh.
        # Empty set for anonymous callers — the field still ships,
        # just always ``false``.
        saved_ids = await get_saved_dish_ids(
            db,
            user_id=user_id,
            dish_ids=[d.id for d in kept_dishes],
        )

        # Review-recall: 24h after a recommendation, if the diner
        # didn't review the dish, drop an in-app notification (D2).
        # Anonymous callers don't have an identity to notify, so we
        # only enqueue for logged-in users. The enqueue is best-effort
        # — a queue write failure should never tank the recommendation
        # response itself.
        logger.warning(
            "DEBUG recommend_dishes: user_id=%s kept_dishes_count=%d",
            user_id,
            len(kept_dishes),
        )
        if user_id is not None and kept_dishes:
            try:
                await enqueue_sommelier_review_recalls(
                    db,
                    user_id=user_id,
                    dish_ids=[d.id for d in kept_dishes],
                )
            except Exception:
                logger.exception(
                    "sommelier review-recall enqueue failed for user %s; "
                    "continuing without recall",
                    user_id,
                )
        result: dict[str, Any] = {
            "count": len(kept_dishes),
            "dishes": [
                _serialize_dish(d, saved_ids=saved_ids)
                for d in kept_dishes
            ],
        }
        if dropped:
            result["allergy_drops"] = dropped
            result["respected_allergies"] = allergies
        # Only surface drop-info when something was actually dropped;
        # keeps the happy path clean for the agent's next iteration.
        if bad_ids:
            result["dropped_ids"] = bad_ids
        if missing_ids:
            result["missing_ids"] = missing_ids
        return result

    return ToolSpec(
        name="recommend_dishes",
        description=(
            "Present a curated subset of dishes as a card grid to the "
            "comensal. Pass 1-6 ``dish_ids`` (UUIDs from a previous "
            "search_dishes call in the same turn). The card grid the "
            "user sees is EXACTLY the dishes you pass here, in this "
            "order — your editorial sentence in the response should "
            "frame these specific dishes. NEVER include uuids that "
            "weren't in a tool output earlier in the turn. NEVER ask "
            "the comensal for a uuid."
        ),
        input_schema=pydantic_to_anthropic_schema(RecommendDishesInput),
        handler=handler,
        emits_card=True,
    )
