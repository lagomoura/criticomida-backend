"""Unit tests for A — Context Injection hint resolver.

Covers the pure-resolution surface of ``build_context_hint``: which
inputs produce a block, which silently drop it (stale URL), and the
dish-wins-over-slug priority. The first-turn-only gate and the
Sommelier-only gate live in ``chat_service.stream_chat`` — tested via
the integration suite where a real conversation row is available.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.chat.client_context import build_context_hint


class _ResultFirst:
    """Shape for ``(await db.execute(stmt)).first()``."""

    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class TestBuildContextHint:
    async def test_returns_none_when_both_inputs_empty(self):
        db = AsyncMock()
        out = await build_context_hint(db)
        assert out is None
        db.execute.assert_not_called()

    async def test_dish_id_resolves_to_dish_and_restaurant_block(self):
        # Row mimics SQLAlchemy named-tuple result of the dish JOIN.
        # ``restaurant_id`` + ``restaurant_slug`` are inlined into the
        # hint so the LLM can pass them straight into tools that need
        # them (e.g. ``search_dishes(restaurant_id=...)``) instead of
        # hallucinating a uuid and burning iterations.
        rest_uuid = _uuid.uuid4()
        row = SimpleNamespace(
            name="Risotto al funghi",
            restaurant_id=rest_uuid,
            restaurant_name="La Vinoteca",
            restaurant_slug="la-vinoteca",
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(row))

        dish_uuid = _uuid.uuid4()
        out = await build_context_hint(db, dish_id=dish_uuid)

        assert out is not None
        assert "Risotto al funghi" in out
        assert "La Vinoteca" in out
        # Identifiers must appear verbatim so the LLM doesn't invent them.
        assert str(rest_uuid) in out
        assert "la-vinoteca" in out
        assert str(dish_uuid) in out
        # The "no es filtro obligatorio" framing keeps the agent from
        # treating the hint as a hard constraint — pin it so the copy
        # doesn't drift to something more imperative.
        assert "pista" in out.lower()

    async def test_restaurant_slug_resolves_to_restaurant_block(self):
        rest_uuid = _uuid.uuid4()
        row = SimpleNamespace(id=rest_uuid, name="Sagardi")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(row))

        out = await build_context_hint(db, restaurant_slug="sagardi-palermo")

        assert out is not None
        assert "Sagardi" in out
        assert str(rest_uuid) in out
        assert "sagardi-palermo" in out

    async def test_dish_wins_over_restaurant_when_both_present(self):
        # ``dish_id`` is the more specific signal; the helper should
        # short-circuit on it and ignore the restaurant slug to keep
        # the prompt single-grounded.
        dish_row = SimpleNamespace(
            name="Pulpo a la gallega",
            restaurant_id=_uuid.uuid4(),
            restaurant_name="Sagardi",
            restaurant_slug="sagardi-palermo",
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(dish_row))

        out = await build_context_hint(
            db,
            restaurant_slug="some-other-place",
            dish_id=_uuid.uuid4(),
        )

        # Only one DB hit (the dish join) — the restaurant lookup is
        # skipped entirely.
        assert db.execute.await_count == 1
        assert out is not None
        assert "Pulpo a la gallega" in out
        assert "Sagardi" in out
        assert "some-other-place" not in out

    async def test_returns_none_when_dish_missing(self):
        # Stale URL: the diner had the tab open on a dish that's been
        # removed. We drop the hint silently rather than prefix a
        # broken block.
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(None))

        out = await build_context_hint(db, dish_id=_uuid.uuid4())

        assert out is None

    async def test_returns_none_when_restaurant_slug_missing(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(None))

        out = await build_context_hint(db, restaurant_slug="ghost-place")

        assert out is None

    async def test_restaurant_id_resolves_to_restaurant_block(self):
        # The restaurant detail route accepts both ``/restaurants/{slug}``
        # and ``/restaurants/{uuid}``. When the diner lands on the UUID
        # form, the launcher sends ``restaurant_id`` directly so the
        # backend can resolve in one query without a slug fallback.
        row = SimpleNamespace(
            name="Eretz Cantina Israelí", slug="eretz-cantina-israeli"
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(row))

        rest_uuid = _uuid.uuid4()
        out = await build_context_hint(db, restaurant_id=rest_uuid)

        assert out is not None
        assert "Eretz Cantina Israelí" in out
        assert str(rest_uuid) in out
        assert "eretz-cantina-israeli" in out

    async def test_restaurant_id_wins_over_slug_when_both_present(self):
        # The FE never sends both, but if a caller does, ``restaurant_id``
        # is the more specific signal (no LIKE / collation surprises) so
        # the helper short-circuits on it.
        row = SimpleNamespace(
            name="Eretz Cantina Israelí", slug="eretz-cantina-israeli"
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(row))

        out = await build_context_hint(
            db,
            restaurant_slug="some-other-place",
            restaurant_id=_uuid.uuid4(),
        )

        assert db.execute.await_count == 1
        assert out is not None
        assert "Eretz Cantina Israelí" in out
        assert "some-other-place" not in out

    async def test_returns_none_when_restaurant_id_missing(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ResultFirst(None))

        out = await build_context_hint(db, restaurant_id=_uuid.uuid4())

        assert out is None
