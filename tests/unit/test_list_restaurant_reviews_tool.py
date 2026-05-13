"""Unit tests for ``list_restaurant_reviews`` (Sommelier-only tool).

B2C mirror of ``list_reviews`` (Business). Two key behavior differences
the tests pin:

- Dynamic scope: the tool resolves the restaurant by uuid / slug / name
  via ``_resolve_restaurant_global``. Ambiguity returns a hint payload,
  no_match returns a hint, missing_input returns a hint — all without
  asking the comensal for the ID.
- Anonymous output: the items never expose ``user_id`` / ``author`` /
  ``display_name`` / ``has_owner_response``. The catalog public reviews
  are read by the comensal anonymously, same as ``get_dish_detail``.

Real DB-backed query execution is exercised by the eval suite — these
tests cover the contract (Pydantic schema + handler validation + the
resolver branches) and the registry wiring.
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.chat import ChatAgent
from app.services.chat.tools._resolution import _resolve_restaurant_global
from app.services.chat.tools._schemas import ListRestaurantReviewsInput
from app.services.chat.tools.registry import build_registry
from app.services.chat.tools.search import make_list_restaurant_reviews_tool


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

    def all(self):
        return list(self._items)


def _make_restaurant(
    name="Eretz Cantina",
    *,
    slug=None,
    location="Palermo",
    city="Buenos Aires",
    rating=4.1,
    review_count=47,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug or name.lower().replace(" ", "-"),
        name=name,
        location_name=location,
        city=city,
        computed_rating=rating,
        review_count=review_count,
    )


def _make_db(execute_returns):
    """Build an AsyncMock DB whose ``execute`` returns the supplied
    sequence of FakeResult objects (one per call, in order)."""
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_FakeResult(items) for items in execute_returns]
    )
    return db


@pytest.fixture
def tool():
    """Default tool: no viewer (safety filter disabled)."""
    return make_list_restaurant_reviews_tool(AsyncMock(), user_id=None)


# ──────────────────────────────────────────────────────────────────────────
#   ListRestaurantReviewsInput — schema contract
# ──────────────────────────────────────────────────────────────────────────


class TestSchemaShape:
    def test_extra_properties_are_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False

    def test_sentiment_is_strict_enum(self, tool):
        prop = tool.input_schema["properties"]["sentiment"]
        assert prop["enum"] == ["any", "positive", "neutral", "negative"]

    def test_sort_enum_includes_most_negative_and_most_positive(self, tool):
        prop = tool.input_schema["properties"]["sort"]
        assert set(prop["enum"]) == {
            "recent",
            "oldest",
            "rating_high",
            "rating_low",
            "most_negative",
            "most_positive",
        }

    def test_limit_bounds_are_one_to_fifty(self, tool):
        prop = tool.input_schema["properties"]["limit"]
        assert prop["minimum"] == 1
        assert prop["maximum"] == 50

    def test_restaurant_name_min_length_is_two(self, tool):
        # Guardrail to prevent fuzzy match against strings like "el"
        # or "a" — those would return hundreds of irrelevant candidates.
        # The schema for a nullable str field is rendered as ``anyOf``;
        # the constraint lives on the string branch.
        prop = tool.input_schema["properties"]["restaurant_name"]
        string_branch = next(
            branch
            for branch in prop["anyOf"]
            if branch.get("type") == "string"
        )
        assert string_branch.get("minLength") == 2

    def test_responded_status_is_absent(self, tool):
        # Sommelier output is anonymous — no exposure of owner replies.
        assert "responded_status" not in tool.input_schema["properties"]


class TestSchemaModelValidator:
    """The ``model_validator`` requires at least one identifier."""

    def test_all_identifiers_missing_raises(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ListRestaurantReviewsInput.model_validate({})

    def test_only_restaurant_id_validates(self):
        inputs = ListRestaurantReviewsInput.model_validate(
            {"restaurant_id": "11111111-1111-1111-1111-111111111111"}
        )
        assert inputs.restaurant_id is not None

    def test_only_restaurant_slug_validates(self):
        inputs = ListRestaurantReviewsInput.model_validate(
            {"restaurant_slug": "eretz-cantina"}
        )
        assert inputs.restaurant_slug == "eretz-cantina"

    def test_only_restaurant_name_validates(self):
        inputs = ListRestaurantReviewsInput.model_validate(
            {"restaurant_name": "Eretz"}
        )
        assert inputs.restaurant_name == "Eretz"

    def test_restaurant_name_too_short_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ListRestaurantReviewsInput.model_validate({"restaurant_name": "e"})


# ──────────────────────────────────────────────────────────────────────────
#   Handler — Pydantic validation surfaces structured errors
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerValidation:
    async def test_missing_identifier_returns_structured_error(self, tool):
        result = await tool.handler({})
        assert "error" in result
        # The error message surfaces the model_validator's text so the
        # LLM has actionable guidance for the next iteration.
        assert "details" in result

    async def test_extra_property_rejected(self, tool):
        result = await tool.handler(
            {"restaurant_slug": "x", "rogue_field": "y"}
        )
        assert "error" in result
        details = result["details"]
        assert any(
            (
                d["loc"][0]
                if isinstance(d["loc"], (list, tuple))
                else d["loc"]
            )
            == "rogue_field"
            for d in details
        )

    async def test_invalid_sentiment_rejected(self, tool):
        result = await tool.handler(
            {"restaurant_slug": "x", "sentiment": "mixed"}
        )
        assert "error" in result

    async def test_invalid_sort_rejected(self, tool):
        result = await tool.handler(
            {"restaurant_slug": "x", "sort": "worst"}
        )
        assert "error" in result

    async def test_min_rating_out_of_range_rejected(self, tool):
        result = await tool.handler(
            {"restaurant_slug": "x", "min_rating": 10}
        )
        assert "error" in result

    async def test_limit_out_of_range_rejected(self, tool):
        result = await tool.handler(
            {"restaurant_slug": "x", "limit": 9999}
        )
        assert "error" in result


# ──────────────────────────────────────────────────────────────────────────
#   Handler — crossed-range guards return useful payloads
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerRangeChecks:
    async def test_min_above_max_rating_returns_invalid_rating_range(
        self, tool
    ):
        # The resolver is never reached when ranges are crossed — the
        # handler short-circuits with the actionable error before any
        # SQL. We assert that the DB was not touched.
        db = AsyncMock()
        db.execute = AsyncMock()
        local_tool = make_list_restaurant_reviews_tool(db, user_id=None)
        result = await local_tool.handler(
            {
                "restaurant_slug": "x",
                "min_rating": 5,
                "max_rating": 2,
            }
        )
        assert result["error"] == "invalid_rating_range"
        assert "5" in result["message"]
        assert "2" in result["message"]
        db.execute.assert_not_called()

    async def test_date_from_after_to_returns_invalid_date_range(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        local_tool = make_list_restaurant_reviews_tool(db, user_id=None)
        result = await local_tool.handler(
            {
                "restaurant_slug": "x",
                "date_from": "2026-05-10",
                "date_to": "2026-01-01",
            }
        )
        assert result["error"] == "invalid_date_range"
        # ISO dates appear in the message so the LLM can correct.
        assert "2026-05-10" in result["message"]
        assert "2026-01-01" in result["message"]
        db.execute.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────
#   _resolve_restaurant_global — direct unit tests
# ──────────────────────────────────────────────────────────────────────────


class TestResolveRestaurantGlobal:
    async def test_missing_all_inputs_returns_missing_input(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id=None,
            restaurant_slug=None,
            restaurant_name=None,
        )
        assert rest is None
        assert payload["error"] == "missing_input"
        db.execute.assert_not_called()

    async def test_uuid_hit_returns_restaurant(self):
        eretz = _make_restaurant("Eretz")
        db = _make_db([[eretz]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id="11111111-1111-1111-1111-111111111111",
            restaurant_slug=None,
            restaurant_name=None,
        )
        assert payload is None
        assert rest is eretz

    async def test_valid_uuid_not_found_no_fallback_returns_error(self):
        db = _make_db([[]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id="11111111-1111-1111-1111-111111111111",
            restaurant_slug=None,
            restaurant_name=None,
        )
        assert rest is None
        assert payload["error"] == "restaurant_not_found"

    async def test_invalid_uuid_falls_back_to_name_search(self):
        # The LLM dumped a name into restaurant_id (forgiveness pattern,
        # same as _resolve_dish_global). The resolver treats it as
        # restaurant_name and runs the ILIKE primary scan, then the
        # accent-insensible Python post-filter.
        eretz = _make_restaurant("Eretz Cantina")
        # Primary ILIKE scan returns one match. The post-filter checks
        # the needle ("Eretz") against the normalized name, which
        # passes — so the resolver returns the dish cleanly.
        db = _make_db([[eretz]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id="Eretz",
            restaurant_slug=None,
            restaurant_name=None,
        )
        assert payload is None
        assert rest is eretz

    async def test_slug_hit_returns_restaurant(self):
        eretz = _make_restaurant("Eretz Cantina", slug="eretz-cantina")
        db = _make_db([[eretz]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id=None,
            restaurant_slug="eretz-cantina",
            restaurant_name=None,
        )
        assert payload is None
        assert rest is eretz

    async def test_slug_miss_no_fallback_returns_slug_not_found(self):
        db = _make_db([[]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id=None,
            restaurant_slug="ghost-cantina",
            restaurant_name=None,
        )
        assert rest is None
        assert payload["error"] == "slug_not_found"

    async def test_unique_name_match_returns_restaurant(self):
        eretz = _make_restaurant("Eretz Cantina Israeli")
        db = _make_db([[eretz]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id=None,
            restaurant_slug=None,
            restaurant_name="Eretz",
        )
        assert payload is None
        assert rest is eretz

    async def test_ambiguous_name_returns_disambiguation(self):
        don_julio_a = _make_restaurant(
            "Don Julio", slug="don-julio", location="Palermo"
        )
        don_julio_b = _make_restaurant(
            "Don Julio Norte", slug="don-julio-norte", location="Belgrano"
        )
        db = _make_db([[don_julio_a, don_julio_b]])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id=None,
            restaurant_slug=None,
            restaurant_name="Don Julio",
        )
        assert rest is None
        assert payload["needs_disambiguation"] is True
        assert payload["query"] == "Don Julio"
        assert len(payload["candidates"]) == 2
        # Candidates carry barrio + city so the comensal can pick.
        assert payload["candidates"][0]["location_name"] == "Palermo"
        assert payload["candidates"][0]["slug"] == "don-julio"

    async def test_no_match_returns_no_match_hint(self):
        # ILIKE primary returns nothing, fallback scan also returns nothing.
        db = _make_db([[], []])
        rest, payload = await _resolve_restaurant_global(
            db,
            restaurant_id=None,
            restaurant_slug=None,
            restaurant_name="Restoran Inexistente XYZ",
        )
        assert rest is None
        assert payload["error"] == "no_match"
        # The query is echoed so the LLM can reuse it when asking the
        # comensal to clarify.
        assert payload["query"] == "Restoran Inexistente XYZ"


# ──────────────────────────────────────────────────────────────────────────
#   Handler — disambiguation / no_match short-circuits
# ──────────────────────────────────────────────────────────────────────────


class TestHandlerResolverHints:
    async def test_disambiguation_hint_is_returned_verbatim(self):
        # When the resolver returns a hint, the handler must surface it
        # unchanged — the LLM uses the structured payload to ask the
        # comensal to pick a candidate without leaking JSON.
        a = _make_restaurant("Don Julio", slug="don-julio")
        b = _make_restaurant("Don Julio Norte", slug="don-julio-norte")
        db = _make_db([[a, b]])
        local_tool = make_list_restaurant_reviews_tool(db, user_id=None)
        result = await local_tool.handler({"restaurant_name": "Don Julio"})
        assert result.get("needs_disambiguation") is True
        # ``reviews`` and ``count`` should NOT be present on a hint —
        # the agent should follow the disambiguation flow first.
        assert "reviews" not in result
        assert "count" not in result


# ──────────────────────────────────────────────────────────────────────────
#   Registry wiring — Sommelier only
# ──────────────────────────────────────────────────────────────────────────


class TestRegistryWiring:
    """The tool must register on the Sommelier toolbelt only —
    Business already has ``list_reviews`` scoped to the owner."""

    def test_registered_for_sommelier(self):
        reg = build_registry(
            agent=ChatAgent.sommelier,
            db=AsyncMock(),
            user_id=None,
            embed_query=None,
        )
        tool_names = {spec.name for spec in reg.tools.values()}
        assert "list_restaurant_reviews" in tool_names

    def test_not_registered_for_business(self):
        reg = build_registry(
            agent=ChatAgent.business,
            db=AsyncMock(),
            user_id=None,
            embed_query=None,
            restaurant_scope_id="11111111-1111-1111-1111-111111111111",
        )
        tool_names = {spec.name for spec in reg.tools.values()}
        assert "list_restaurant_reviews" not in tool_names
        # The Business analog stays in place.
        assert "list_reviews" in tool_names

    def test_not_registered_for_ghostwriter(self):
        reg = build_registry(
            agent=ChatAgent.ghostwriter,
            db=AsyncMock(),
            user_id=None,
            embed_query=None,
        )
        tool_names = {spec.name for spec in reg.tools.values()}
        assert "list_restaurant_reviews" not in tool_names


# ──────────────────────────────────────────────────────────────────────────
#   Tool metadata
# ──────────────────────────────────────────────────────────────────────────


class TestToolMetadata:
    def test_tool_does_not_emit_card(self, tool):
        # Data-only, same as ``get_dish_detail`` and ``search_dishes``.
        # The agent reads the JSON and writes editorial text — no card
        # is rendered automatically.
        assert tool.emits_card is False

    def test_description_mentions_anonymous_output(self, tool):
        # The contract description must remind the LLM that the output
        # has no author — defense in depth alongside the prompt.
        assert "anónimo" in tool.description.lower() or "anonymous" in tool.description.lower()

    def test_description_mentions_when_not_to_use(self, tool):
        # Single composable tool covers many questions — the LLM needs
        # explicit guidance to pick search_dishes / get_dish_detail for
        # adjacent jobs.
        assert "search_dishes" in tool.description
        assert "get_dish_detail" in tool.description
