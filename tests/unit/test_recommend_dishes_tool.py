"""Unit tests for ``recommend_dishes`` — the curated-grid presenter.

The tool is the only path through which the comensal sees dish cards
in the Sommelier. The tests pin the input contract (1-6 valid uuids,
extra=forbid) and the error paths the agent loop relies on for
recovery: bad uuid → ``no_valid_ids``, all uuids missing in DB →
``no_match`` with sample diagnostics. Real DB-backed happy-path lookup
is exercised by the eval suite where the rows are seeded.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.services.chat.tools._schemas import RecommendDishesInput
from app.services.chat.tools.recommend import make_recommend_dishes_tool


# ──────────────────────────────────────────────────────────────────────────
#   Fakes — minimal SQLAlchemy-shape doubles
# ──────────────────────────────────────────────────────────────────────────


class _FakeScalars:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return _FakeScalars(self._items)


def _make_dish(name: str = "Café Turco"):
    return SimpleNamespace(
        id=_uuid.uuid4(),
        name=name,
        description=None,
        cover_image_url=None,
        computed_rating=4.5,
        review_count=10,
        price_tier=None,
        restaurant=SimpleNamespace(
            id=_uuid.uuid4(),
            slug="eretz",
            name="Eretz Cantina Israeli",
            location_name="Palermo",
            city="Buenos Aires",
            latitude=-34.59,
            longitude=-58.42,
            category=SimpleNamespace(name="Israelí"),
            has_reservation=False,
            is_claimed=False,
        ),
    )


# ──────────────────────────────────────────────────────────────────────────
#   Schema-level validation
# ──────────────────────────────────────────────────────────────────────────


class TestRecommendDishesSchema:
    def test_accepts_one_to_six_uuids(self):
        ids = [str(_uuid.uuid4()) for _ in range(3)]
        inputs = RecommendDishesInput.model_validate({"dish_ids": ids})
        assert inputs.dish_ids == ids

    def test_zero_dishes_rejected(self):
        with pytest.raises(ValidationError):
            RecommendDishesInput.model_validate({"dish_ids": []})

    def test_seven_dishes_rejected(self):
        with pytest.raises(ValidationError):
            RecommendDishesInput.model_validate(
                {"dish_ids": [str(_uuid.uuid4()) for _ in range(7)]}
            )

    def test_extra_property_rejected(self):
        with pytest.raises(ValidationError):
            RecommendDishesInput.model_validate(
                {"dish_ids": [str(_uuid.uuid4())], "rogue": "x"}
            )

    def test_missing_dish_ids_rejected(self):
        with pytest.raises(ValidationError):
            RecommendDishesInput.model_validate({})


# ──────────────────────────────────────────────────────────────────────────
#   Handler error paths (no DB needed)
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerErrors:
    @pytest.fixture
    def tool(self):
        return make_recommend_dishes_tool(AsyncMock())

    async def test_invalid_arg_returns_validation_error(self, tool):
        result = await tool.handler({"dish_ids": []})
        assert "error" in result
        assert "details" in result

    async def test_unparseable_uuids_returns_no_valid_ids(self, tool):
        result = await tool.handler(
            {"dish_ids": ["not-a-uuid", "also-bad"]}
        )
        assert result["error"] == "no_valid_ids"
        assert set(result["dropped_ids"]) == {"not-a-uuid", "also-bad"}

    async def test_all_uuids_missing_in_db_returns_no_match(self):
        # DB returns no rows for any of the queried ids.
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult([]))
        tool = make_recommend_dishes_tool(db)
        good_uuid = str(_uuid.uuid4())
        result = await tool.handler({"dish_ids": [good_uuid]})
        assert result["error"] == "no_match"
        assert good_uuid in result["missing_ids"]


# ──────────────────────────────────────────────────────────────────────────
#   Happy path — preserves agent order
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerHappyPath:
    async def test_preserves_agent_passed_order(self):
        d1 = _make_dish("Café Turco")
        d2 = _make_dish("Risotto de Hongos")
        d3 = _make_dish("Pizza Margherita")
        # DB returns rows in random order — the handler must reorder
        # to match the uuids the agent passed.
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult([d3, d1, d2]))
        tool = make_recommend_dishes_tool(db)

        ordered_ids = [str(d1.id), str(d2.id), str(d3.id)]
        result = await tool.handler({"dish_ids": ordered_ids})

        assert "error" not in result
        assert result["count"] == 3
        names = [d["name"] for d in result["dishes"]]
        assert names == ["Café Turco", "Risotto de Hongos", "Pizza Margherita"]

    async def test_dedupes_repeated_ids(self):
        d1 = _make_dish("Café Turco")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_FakeResult([d1]))
        tool = make_recommend_dishes_tool(db)

        result = await tool.handler(
            {"dish_ids": [str(d1.id), str(d1.id), str(d1.id)]}
        )
        assert result["count"] == 1
