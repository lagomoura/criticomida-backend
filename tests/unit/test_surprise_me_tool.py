"""Unit tests for ``surprise_me``.

The tool's job is to pick ONE high-rated dish OUTSIDE the comensal's
reviewed history while respecting allergies, and to do it
deterministically per (user, day) so a repeated call doesn't rotate
suggestions in the same session. Tests pin:

- Schema (extra forbid, neighborhood optional).
- Allergy → category blocklist mapping.
- Determinism: same (user, day) → same dish, twice in a row.
- Empty pool → ``no_match`` payload.
- Anonymous: still works, just less personalised.

The full happy-path (real DB rows + profile aggregation) is
exercised by the eval suite where the fixture seeds dishes across
multiple neighborhoods + categories.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.services.chat.tools._schemas import SurpriseMeInput
from app.services.chat.tools.discovery import (
    _ALLERGY_CATEGORY_BLOCKLIST,
    _blocked_categories_for,
    make_surprise_me_tool,
)


# ──────────────────────────────────────────────────────────────────────────
#   Fakes
# ──────────────────────────────────────────────────────────────────────────


class _FakeScalars:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return _FakeScalars(self._items)


def _make_dish(
    name: str,
    *,
    rating: float = 4.6,
    category_slug: str = "italiana",
    neighborhood: str = "Palermo",
):
    cat = SimpleNamespace(slug=category_slug, name=category_slug.capitalize())
    rest = SimpleNamespace(
        id=_uuid.uuid4(),
        slug=neighborhood.lower(),
        name=f"{name} place",
        location_name=neighborhood,
        category=cat,
    )
    return SimpleNamespace(
        id=_uuid.uuid4(),
        name=name,
        computed_rating=rating,
        review_count=10,
        restaurant=rest,
    )


# ──────────────────────────────────────────────────────────────────────────
#   Schema
# ──────────────────────────────────────────────────────────────────────────


class TestSurpriseMeSchema:
    def test_no_args_validates(self):
        inputs = SurpriseMeInput.model_validate({})
        assert inputs.neighborhood is None

    def test_with_neighborhood(self):
        inputs = SurpriseMeInput.model_validate({"neighborhood": "Palermo"})
        assert inputs.neighborhood == "Palermo"

    def test_alias_barrio_accepted(self):
        inputs = SurpriseMeInput.model_validate({"barrio": "Centro"})
        assert inputs.neighborhood == "Centro"

    def test_extra_property_rejected(self):
        with pytest.raises(ValidationError):
            SurpriseMeInput.model_validate({"rogue": "x"})


# ──────────────────────────────────────────────────────────────────────────
#   Allergy blocklist
# ──────────────────────────────────────────────────────────────────────────


class TestAllergyBlocklist:
    def test_celiac_blocks_italian_burgers(self):
        blocked = _blocked_categories_for(["gluten"])
        assert "italiana" in blocked
        assert "burgers" in blocked

    def test_lactose_blocks_helados(self):
        blocked = _blocked_categories_for(["lácteo"])
        assert "helados" in blocked

    def test_substring_match(self):
        # "alérgico al gluten" still picks up the wheat blocklist.
        blocked = _blocked_categories_for(["alérgico al gluten"])
        assert "italiana" in blocked

    def test_unknown_allergy_no_blocks(self):
        blocked = _blocked_categories_for(["rojo"])
        assert blocked == set()

    def test_blocklist_is_consistent(self):
        # Sanity: every value in the table is a set of strings.
        for value in _ALLERGY_CATEGORY_BLOCKLIST.values():
            assert isinstance(value, set)
            assert all(isinstance(v, str) for v in value)


# ──────────────────────────────────────────────────────────────────────────
#   Handler — error paths
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerErrors:
    async def test_invalid_arg_returns_validation_error(self):
        db = AsyncMock()
        tool = make_surprise_me_tool(db, user_id=None)
        result = await tool.handler({"rogue": "x"})
        assert "error" in result
        assert "details" in result

    async def test_empty_pool_returns_no_match(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult([]))
        tool = make_surprise_me_tool(db, user_id=None)
        result = await tool.handler({})
        assert result["error"] == "no_match"


# ──────────────────────────────────────────────────────────────────────────
#   Handler — happy path
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerHappyPath:
    async def test_anonymous_returns_a_pick(self):
        d = _make_dish("Café Turco", category_slug="israeli", neighborhood="Palermo")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult([d]))
        tool = make_surprise_me_tool(db, user_id=None)
        result = await tool.handler({})
        assert "error" not in result
        assert result["name"] == "Café Turco"
        assert result["serendipity_reason"]

    async def test_deterministic_per_user_day(self):
        # Two dishes, both rated equally — pick must be the same on
        # two back-to-back calls with the same user_id.
        d1 = _make_dish("A", category_slug="parrilla", neighborhood="Centro")
        d2 = _make_dish("B", category_slug="japonesa", neighborhood="Belgrano")
        user = _uuid.uuid4()

        db1 = AsyncMock()
        db1.execute = AsyncMock(return_value=_FakeResult([d1, d2]))
        tool1 = make_surprise_me_tool(db1, user_id=user)
        with patch(
            "app.services.chat.tools.discovery.get_taste_profile",
            new=AsyncMock(return_value=None),
        ):
            r1 = await tool1.handler({})

        db2 = AsyncMock()
        db2.execute = AsyncMock(return_value=_FakeResult([d1, d2]))
        tool2 = make_surprise_me_tool(db2, user_id=user)
        with patch(
            "app.services.chat.tools.discovery.get_taste_profile",
            new=AsyncMock(return_value=None),
        ):
            r2 = await tool2.handler({})

        assert r1["dish_id"] == r2["dish_id"]

    async def test_novelty_filter_prefers_outside_history(self):
        # Profile has top_categories=['italiana'], top_neighborhoods=['Palermo'].
        # One candidate is italian-Palermo (familiar), the other parrilla-Centro
        # (novel). Surprise must pick the parrilla.
        familiar = _make_dish(
            "Pasta", category_slug="italiana", neighborhood="Palermo"
        )
        novel = _make_dish(
            "Bife", category_slug="parrilla", neighborhood="Centro"
        )
        profile = SimpleNamespace(
            top_categories=["italiana"],
            top_neighborhoods=["Palermo"],
            allergies=[],
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult([familiar, novel]))
        tool = make_surprise_me_tool(db, user_id=_uuid.uuid4())
        with patch(
            "app.services.chat.tools.discovery.get_taste_profile",
            new=AsyncMock(return_value=profile),
        ):
            result = await tool.handler({})
        assert result["name"] == "Bife"
        assert "serendipity_reason" in result
