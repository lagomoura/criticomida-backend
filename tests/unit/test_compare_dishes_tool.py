"""Unit tests for ``compare_dishes``.

Pin the input contract (2-4 dishes by uuid OR by name, extra forbid)
and the resolver-error short-circuit (a single ambiguous name aborts
the comparison so the agent can disambiguate before half a grid
renders). Real DB-backed pillar/pros aggregation is exercised by the
eval suite where the fixture seeds reviews + pros_cons.
"""

from __future__ import annotations

import uuid as _uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.services.chat.tools._schemas import CompareDishesInput
from app.services.chat.tools.discovery import make_compare_dishes_tool


# ──────────────────────────────────────────────────────────────────────────
#   Schema
# ──────────────────────────────────────────────────────────────────────────


class TestCompareDishesSchema:
    def test_two_uuids_validates(self):
        ids = [str(_uuid.uuid4()) for _ in range(2)]
        inputs = CompareDishesInput.model_validate({"dish_ids": ids})
        assert inputs.dish_ids == ids
        assert inputs.dish_names is None

    def test_two_names_validates(self):
        inputs = CompareDishesInput.model_validate(
            {"dish_names": ["risotto", "carbonara"]}
        )
        assert inputs.dish_names == ["risotto", "carbonara"]

    def test_one_dish_rejected(self):
        with pytest.raises(ValidationError):
            CompareDishesInput.model_validate({"dish_ids": [str(_uuid.uuid4())]})

    def test_five_dishes_rejected(self):
        with pytest.raises(ValidationError):
            CompareDishesInput.model_validate(
                {"dish_ids": [str(_uuid.uuid4()) for _ in range(5)]}
            )

    def test_extra_property_rejected(self):
        with pytest.raises(ValidationError):
            CompareDishesInput.model_validate(
                {"dish_ids": [str(_uuid.uuid4()) for _ in range(2)], "rogue": "x"}
            )


# ──────────────────────────────────────────────────────────────────────────
#   Handler error paths
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerErrors:
    @pytest.fixture
    def tool(self):
        return make_compare_dishes_tool(AsyncMock())

    async def test_no_input_returns_missing_input(self, tool):
        # Schema doesn't require either field at the schema level — both
        # nullable — so the handler is responsible for the "at least
        # one" check and surfacing a friendly message.
        result = await tool.handler({})
        assert result["error"] == "missing_input"

    async def test_validation_error_passthrough(self, tool):
        result = await tool.handler({"dish_ids": [str(_uuid.uuid4())]})
        assert "error" in result
        assert "details" in result

    async def test_resolver_failure_aborts_comparison(self):
        # ``compare_dishes`` aborts as soon as one slot fails to
        # resolve — half a grid would mislead the comensal more than
        # a clean disambiguation prompt. The error payload from the
        # resolver carries through, plus context about what was
        # already resolved.
        good_dish = SimpleNamespace(
            id=_uuid.uuid4(),
            name="Café Turco",
            cover_image_url=None,
            computed_rating=4.5,
            review_count=10,
            price_tier=None,
            restaurant=SimpleNamespace(
                id=_uuid.uuid4(),
                slug="eretz",
                name="Eretz",
                location_name="Palermo",
                latitude=None,
                longitude=None,
            ),
        )

        async def fake_resolver(db, **kwargs):
            # Slot 1 resolves; slot 2 returns ambiguity.
            if kwargs.get("dish_name") == "café":
                return good_dish, None
            return None, {
                "needs_disambiguation": True,
                "candidates": [],
                "message": "ambiguous",
            }

        db = AsyncMock()
        # Patch the helper so we don't need a real DB.
        with patch(
            "app.services.chat.tools.discovery._resolve_dish_global",
            new=fake_resolver,
        ):
            tool = make_compare_dishes_tool(db)
            result = await tool.handler(
                {"dish_names": ["café", "ambiguo"]}
            )
        assert result.get("needs_disambiguation") is True
        # The unresolved slot is annotated for the agent.
        assert result["unresolved_slot"]["dish_name"] == "ambiguo"
        # And the slot we DID resolve is recorded so the agent has
        # context for the disambiguation prompt.
        assert result["resolved_so_far"][0]["name"] == "Café Turco"
