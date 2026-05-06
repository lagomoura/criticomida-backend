"""Shared helper: look up which dishes the comensal already has in
their want-to-try list.

Tools that emit cards (``recommend_dishes``, ``compare_dishes``)
need to surface the bookmark state per dish so the FE can paint the
correct chip on first render. Without this lookup the bookmark UI
defaults to "Quiero probar" even after the comensal saved a dish in
a previous session — the local React state resets on every refresh,
so the truth has to come from the server.

The query is one ``IN`` lookup against ``want_to_try_dishes`` per
tool call (cheap; the dish_ids list is at most 6).
"""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import WantToTryDish


async def get_saved_dish_ids(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    dish_ids: Iterable[uuid.UUID],
) -> set[uuid.UUID]:
    """Return the subset of ``dish_ids`` that the comensal already
    added to their wishlist. Empty set when there's no auth context
    or no ids to look up — the caller should treat that as "all
    bookmarks default to off"."""
    ids = list(dish_ids)
    if user_id is None or not ids:
        return set()
    stmt = select(WantToTryDish.dish_id).where(
        WantToTryDish.user_id == user_id,
        WantToTryDish.dish_id.in_(ids),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return set(rows)
