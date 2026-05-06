"""Unit tests for ``_resolve_dish_global`` — the shared defensive
resolver used by the Sommelier (global catalog) and Business (single
restaurant scope).

The point of the resolver is structural: even if the LLM ignores
Regla #0 and dumps a name into ``dish_id`` (or asks the human for a
UUID), the tool short-circuits to a useful, LLM-readable payload. We
test each branch of that contract — happy path, disambiguation,
no-match-with-menu-peek, no-match-global-with-search-hint, missing
input — without standing up a real DB.

Real query execution is exercised by the eval suite (Phase 3 of the
Sommelier upgrade plan) on the integration database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.chat.tools._resolution import _resolve_dish_global


# ──────────────────────────────────────────────────────────────────────────
#   Fakes — minimal stand-ins for SQLAlchemy result objects
# ──────────────────────────────────────────────────────────────────────────


class _FakeScalars:
    def __init__(self, items):
        self._items = list(items)

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return _FakeScalars(self._items)


def _make_restaurant(name="Trattoria X", location="Palermo"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=name.lower().replace(" ", "-"),
        name=name,
        location_name=location,
        city="Buenos Aires",
    )


def _make_dish(name, *, review_count=10, rating=4.5, restaurant=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        review_count=review_count,
        computed_rating=rating,
        restaurant=restaurant or _make_restaurant(),
    )


def _make_db(execute_returns):
    """Build an AsyncMock DB whose ``execute`` calls return the given
    sequence of FakeResult objects (one per call, in order)."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_FakeResult(items) for items in execute_returns])
    return db


# ──────────────────────────────────────────────────────────────────────────
#   Branches that don't hit the DB — the cheap defensive layer
# ──────────────────────────────────────────────────────────────────────────


class TestNoDBCalls:
    async def test_missing_both_inputs_returns_missing_input(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        dish, payload = await _resolve_dish_global(
            db, restaurant_scope_id=None, dish_id=None, dish_name=None
        )
        assert dish is None
        assert payload["error"] == "missing_input"
        # The message must mention ``dish_name``/``dish_id`` so the LLM
        # has actionable guidance for the next iteration.
        assert "dish_name" in payload["message"]
        assert "dish_id" in payload["message"]
        # And the actor word should fit the agent (default = comensal).
        assert "comensal" in payload["message"]
        # Critically: NO db.execute calls happened.
        db.execute.assert_not_called()

    async def test_empty_dish_name_returns_missing_input(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        dish, payload = await _resolve_dish_global(
            db, restaurant_scope_id=None, dish_id=None, dish_name="   "
        )
        assert dish is None
        assert payload["error"] == "missing_input"
        db.execute.assert_not_called()

    async def test_owner_actor_changes_message_phrasing(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        _, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id="r1",
            dish_id=None,
            dish_name=None,
            actor="owner",
        )
        assert "owner" in payload["message"]
        assert "comensal" not in payload["message"]


# ──────────────────────────────────────────────────────────────────────────
#   Name-search branches — DB returns curated rows
# ──────────────────────────────────────────────────────────────────────────


class TestUniqueMatch:
    async def test_global_unique_match_returns_dish(self):
        risotto = _make_dish("Risotto de Hongos")
        # Global path with one ILIKE hit.
        db = _make_db([[risotto]])
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id=None,
            dish_name="risotto",
        )
        assert payload is None
        assert dish is risotto

    async def test_scoped_unique_match_returns_dish(self):
        risotto = _make_dish("Risotto de Hongos")
        # Scoped path pulls the whole menu (one query).
        db = _make_db([[risotto]])
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id="r1",
            dish_id=None,
            dish_name="risotto",
            actor="owner",
        )
        assert payload is None
        assert dish is risotto


class TestDisambiguation:
    async def test_multiple_matches_returns_disambiguation_with_candidates(self):
        risotto_a = _make_dish(
            "Risotto de Hongos",
            restaurant=_make_restaurant("Trattoria A", "Palermo"),
        )
        risotto_b = _make_dish(
            "Risotto al Funghi",
            restaurant=_make_restaurant("Bistró B", "Belgrano"),
        )
        db = _make_db([[risotto_a, risotto_b]])
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id=None,
            dish_name="risotto",
        )
        assert dish is None
        assert payload["needs_disambiguation"] is True
        assert payload["query"] == "risotto"
        assert len(payload["candidates"]) == 2
        # Global path: candidates carry restaurant + neighborhood so
        # the comensal can tell two risottos apart at a glance.
        assert payload["candidates"][0]["restaurant_name"] == "Trattoria A"
        assert payload["candidates"][0]["location_name"] == "Palermo"

    async def test_scoped_disambiguation_omits_restaurant_redundantly(self):
        # Inside a single restaurant the candidate's restaurant is
        # implicit — keep the payload tight.
        a = _make_dish("Hamburguesa Clásica")
        b = _make_dish("Hamburguesa Vegana")
        db = _make_db([[a, b]])
        _, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id="r1",
            dish_id=None,
            dish_name="hamburguesa",
            actor="owner",
        )
        assert payload["needs_disambiguation"] is True
        assert "restaurant_name" not in payload["candidates"][0]


class TestNoMatch:
    async def test_scoped_no_match_returns_menu_peek(self):
        # Menu has dishes, but none match the query.
        a = _make_dish("Pasta Carbonara")
        b = _make_dish("Tiramisú")
        db = _make_db([[a, b]])
        _, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id="r1",
            dish_id=None,
            dish_name="risotto",
            actor="owner",
        )
        assert payload["error"] == "no_match"
        assert payload["query"] == "risotto"
        # menu_peek includes the actual menu so the LLM can offer
        # alternatives instead of inventing one.
        assert {p["name"] for p in payload["menu_peek"]} == {
            "Pasta Carbonara",
            "Tiramisú",
        }

    async def test_scoped_empty_menu_returns_no_dishes_registered(self):
        db = _make_db([[]])
        _, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id="r1",
            dish_id=None,
            dish_name="risotto",
            actor="owner",
        )
        assert payload["error"] == "no_dishes_registered"

    async def test_global_no_match_suggests_semantic_search_fallback(self):
        # ILIKE returns nothing AND the wider scan also returns nothing.
        db = _make_db([[], []])
        _, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id=None,
            dish_name="quesadilla mole",
        )
        assert payload["error"] == "no_match"
        # Critical: the message must point the LLM at search_dishes
        # with semantic_query — that's the only way to recover when a
        # plato exists under a different exact name.
        assert "search_dishes" in payload["message"]
        assert "semantic_query" in payload["message"]
        assert "quesadilla mole" in payload["message"]


# ──────────────────────────────────────────────────────────────────────────
#   UUID paths — exhaustive on the boundary cases
# ──────────────────────────────────────────────────────────────────────────


class TestUUIDPath:
    async def test_invalid_uuid_falls_through_to_name_search(self):
        # The LLM passed "hamburguesa" in dish_id (a common slip
        # when Regla #0 doesn't fully land). The resolver should
        # treat it as a name and search.
        match = _make_dish("Hamburguesa Clásica")
        db = _make_db([[match]])
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id="hamburguesa",
            dish_name=None,
        )
        assert payload is None
        assert dish is match

    async def test_valid_uuid_not_in_global_returns_dish_not_found(self):
        # First call (UUID lookup) returns nothing, no name to fall back to.
        db = _make_db([[]])
        good_uuid = "11111111-1111-1111-1111-111111111111"
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id=good_uuid,
            dish_name=None,
        )
        assert dish is None
        assert payload["error"] == "dish_not_found"

    async def test_valid_uuid_not_in_scope_returns_dish_not_in_scope(self):
        # Owner path: UUID is valid but the dish doesn't belong to
        # this restaurant. The wrapper in business.py relies on this
        # exact branch to enforce defense-in-depth.
        db = _make_db([[]])
        good_uuid = "11111111-1111-1111-1111-111111111111"
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id="r1",
            dish_id=good_uuid,
            dish_name=None,
            actor="owner",
        )
        assert dish is None
        assert payload["error"] == "dish_not_in_scope"

    async def test_uuid_hit_returns_dish(self):
        risotto = _make_dish("Risotto")
        db = _make_db([[risotto]])
        good_uuid = "11111111-1111-1111-1111-111111111111"
        dish, payload = await _resolve_dish_global(
            db,
            restaurant_scope_id=None,
            dish_id=good_uuid,
            dish_name=None,
        )
        assert payload is None
        assert dish is risotto
