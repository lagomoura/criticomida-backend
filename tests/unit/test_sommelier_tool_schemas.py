"""Unit tests for the Sommelier tool input schemas.

Each Sommelier tool now validates its arguments through a Pydantic
model in ``app.services.chat.tools._schemas``. The contract is the
defensive layer: out-of-range pillars, invalid price tiers, missing
required fields all surface as ``{"error": ..., "details": [...]}``
that the agent loop hands back to the model so it can correct on the
next iteration.

These tests pin that contract — happy path + the most common ways the
LLM could shoot itself in the foot. Real DB-backed paths (search
returning rows, ratings shape, etc.) are exercised by the eval suite
(Phase 3 of the Sommelier upgrade plan).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.services.chat.tools._schemas import (
    AddToWishlistInput,
    BboxFilter,
    CenterPoint,
    CreateDishRouteInput,
    GetDishDetailInput,
    ListRestaurantReviewsInput,
    OpenInMapInput,
    PriceTierFilter,
    RequestReservationInput,
    SearchDishesInput,
)
from app.services.chat.tools.map import make_open_in_map_tool
from app.services.chat.tools.routes import make_create_dish_route_tool
from app.services.chat.tools.reservations import make_request_reservation_tool
from app.services.chat.tools.search import (
    make_list_restaurant_reviews_tool,
    make_search_dishes_tool,
)


# ──────────────────────────────────────────────────────────────────────────
#   SearchDishesInput — workhorse of the Sommelier
# ──────────────────────────────────────────────────────────────────────────


class TestSearchDishesInput:
    def test_happy_path_with_all_filters(self):
        inputs = SearchDishesInput.model_validate(
            {
                "neighborhood": "Palermo",
                "city": "Buenos Aires",
                "min_presentation": 3,
                "min_rating": 4,
                "max_price_tier": "$$",
                "category_slug": "italiana",
                "semantic_query": "primera cita",
                "limit": 6,
            }
        )
        assert inputs.neighborhood == "Palermo"
        assert inputs.max_price_tier is PriceTierFilter.mid
        assert inputs.limit == 6

    def test_pillar_above_three_rejected(self):
        with pytest.raises(ValidationError):
            SearchDishesInput.model_validate({"min_value_prop": 5})

    def test_pillar_below_one_rejected(self):
        with pytest.raises(ValidationError):
            SearchDishesInput.model_validate({"min_presentation": 0})

    def test_rating_above_five_rejected(self):
        with pytest.raises(ValidationError):
            SearchDishesInput.model_validate({"min_rating": 6})

    def test_extra_property_rejected(self):
        with pytest.raises(ValidationError):
            SearchDishesInput.model_validate(
                {"neighborhood": "Centro", "rogue_field": "x"}
            )

    def test_price_tier_synonym_low_normalises(self):
        inputs = SearchDishesInput.model_validate({"max_price_tier": "low"})
        assert inputs.max_price_tier is PriceTierFilter.cheap

    def test_price_tier_synonym_barato_normalises(self):
        inputs = SearchDishesInput.model_validate({"max_price_tier": "barato"})
        assert inputs.max_price_tier is PriceTierFilter.cheap

    def test_price_tier_overshoot_clamps_to_high(self):
        # "$$$$" doesn't exist in the catalog — the normaliser folds it
        # onto the highest bucket the schema models so the LLM doesn't
        # bounce off a hard rejection on a benign overshoot.
        inputs = SearchDishesInput.model_validate({"max_price_tier": "$$$$"})
        assert inputs.max_price_tier is PriceTierFilter.high

    def test_unknown_price_tier_string_rejected(self):
        with pytest.raises(ValidationError):
            SearchDishesInput.model_validate({"max_price_tier": "free"})

    def test_neighborhood_alias_barrio_accepted(self):
        inputs = SearchDishesInput.model_validate({"barrio": "Palermo"})
        assert inputs.neighborhood == "Palermo"

    def test_semantic_query_alias_mood_accepted(self):
        inputs = SearchDishesInput.model_validate({"mood": "comida confort"})
        assert inputs.semantic_query == "comida confort"

    def test_limit_default_is_six(self):
        inputs = SearchDishesInput.model_validate({})
        assert inputs.limit == 6

    def test_limit_above_twelve_rejected(self):
        with pytest.raises(ValidationError):
            SearchDishesInput.model_validate({"limit": 50})

    def test_name_contains_validates(self):
        inputs = SearchDishesInput.model_validate({"name_contains": "ceviche"})
        assert inputs.name_contains == "ceviche"

    def test_name_contains_alias_plato_accepted(self):
        # El LLM en español tiende a tirar "plato" / "nombre" como key —
        # los aceptamos para no rebotar payloads benignos.
        inputs = SearchDishesInput.model_validate({"plato": "ramen"})
        assert inputs.name_contains == "ramen"

    def test_name_contains_alias_dish_name_accepted(self):
        inputs = SearchDishesInput.model_validate({"dish_name": "risotto"})
        assert inputs.name_contains == "risotto"


class TestSearchDishesHandler:
    @pytest.fixture
    def tool(self):
        return make_search_dishes_tool(AsyncMock(), embed_query=None)

    async def test_invalid_arg_returns_structured_error(self, tool):
        result = await tool.handler({"min_value_prop": 99})
        assert "error" in result
        assert "details" in result
        # Caller (agent loop) needs path info to relay back to the model.
        assert any(
            "min_value_prop"
            in (d["loc"][0] if isinstance(d["loc"], (list, tuple)) else d["loc"])
            for d in result["details"]
        )

    async def test_restaurant_id_field_is_accepted(self, tool):
        # The field is wired through the schema; it parses cleanly as a
        # UUID string. The handler resolution happens against the DB so
        # we don't exercise the full path here — that's covered by the
        # integration suite. This test just pins the contract.
        prop = tool.input_schema["properties"]["restaurant_id"]
        # Nullable str → anyOf with a string branch.
        assert any(b.get("type") == "string" for b in prop["anyOf"])

    async def test_restaurant_id_invalid_uuid_returns_structured_error(
        self, tool
    ):
        # The LLM dumped a name into restaurant_id (forgivable slip).
        # The handler short-circuits with an actionable error rather than
        # silently scanning the whole catalog. Empty dishes prevents the
        # comensal from seeing random cards.
        result = await tool.handler({"restaurant_id": "not-a-uuid"})
        assert "error" in result
        assert "UUID" in result["error"]
        assert result["count"] == 0
        assert result["dishes"] == []


# ──────────────────────────────────────────────────────────────────────────
#   GetDishDetailInput / AddToWishlistInput — both accept name OR id
# ──────────────────────────────────────────────────────────────────────────


class TestGetDishDetailInput:
    def test_dish_id_alone_validates(self):
        inputs = GetDishDetailInput.model_validate(
            {"dish_id": "11111111-1111-1111-1111-111111111111"}
        )
        assert inputs.dish_id is not None
        assert inputs.dish_name is None

    def test_dish_name_alone_validates(self):
        inputs = GetDishDetailInput.model_validate({"dish_name": "el risotto"})
        assert inputs.dish_name == "el risotto"
        assert inputs.dish_id is None

    def test_both_empty_validates_at_schema_level(self):
        # Schema doesn't require either — the handler gates that check
        # so it can issue the friendly "missing_input" payload instead
        # of the bare ValidationError. Both nullable here is correct.
        inputs = GetDishDetailInput.model_validate({})
        assert inputs.dish_id is None
        assert inputs.dish_name is None

    def test_extra_property_rejected(self):
        with pytest.raises(ValidationError):
            GetDishDetailInput.model_validate({"dish_name": "x", "rogue": "y"})


class TestAddToWishlistInput:
    def test_dish_name_validates(self):
        inputs = AddToWishlistInput.model_validate({"dish_name": "risotto"})
        assert inputs.dish_name == "risotto"

    def test_extra_property_rejected(self):
        with pytest.raises(ValidationError):
            AddToWishlistInput.model_validate({"dish_id": "x", "rogue": "y"})


# ──────────────────────────────────────────────────────────────────────────
#   OpenInMapInput
# ──────────────────────────────────────────────────────────────────────────


class TestOpenInMapInput:
    def test_bbox_validates(self):
        inputs = OpenInMapInput.model_validate(
            {"bbox": {"south": -35, "west": -59, "north": -34, "east": -58}}
        )
        assert isinstance(inputs.bbox, BboxFilter)
        assert inputs.bbox.south == -35

    def test_center_with_zoom_validates(self):
        inputs = OpenInMapInput.model_validate(
            {"center": {"lat": -34.6, "lng": -58.4, "zoom": 14}}
        )
        assert isinstance(inputs.center, CenterPoint)
        assert inputs.center.zoom == 14

    def test_dish_ids_list_validates(self):
        inputs = OpenInMapInput.model_validate(
            {"dish_ids": ["11111111-1111-1111-1111-111111111111"]}
        )
        assert inputs.dish_ids == ["11111111-1111-1111-1111-111111111111"]

    def test_lat_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            OpenInMapInput.model_validate({"center": {"lat": 999, "lng": 0}})

    def test_zoom_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            OpenInMapInput.model_validate(
                {"center": {"lat": 0, "lng": 0, "zoom": 25}}
            )


class TestOpenInMapHandler:
    @pytest.fixture
    def tool(self):
        return make_open_in_map_tool()

    async def test_no_input_returns_error(self, tool):
        result = await tool.handler({})
        assert "error" in result

    async def test_dish_ids_normalises_payload(self, tool):
        result = await tool.handler(
            {"dish_ids": ["11111111-1111-1111-1111-111111111111"]}
        )
        assert result["action"] == "open_in_map"
        assert result["dish_ids"] == ["11111111-1111-1111-1111-111111111111"]
        assert result["bbox"] is None
        assert result["center"] is None


# ──────────────────────────────────────────────────────────────────────────
#   CreateDishRouteInput
# ──────────────────────────────────────────────────────────────────────────


class TestCreateDishRouteInput:
    def test_happy_path(self):
        inputs = CreateDishRouteInput.model_validate(
            {
                "name": "Domingo en el Centro",
                "dish_ids": [
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ],
            }
        )
        assert inputs.is_public is True  # default
        assert len(inputs.dish_ids) == 2

    def test_one_dish_rejected(self):
        with pytest.raises(ValidationError):
            CreateDishRouteInput.model_validate(
                {"name": "Solo", "dish_ids": ["uuid1"]}
            )

    def test_eleven_dishes_rejected(self):
        with pytest.raises(ValidationError):
            CreateDishRouteInput.model_validate(
                {"name": "Demasiado", "dish_ids": [f"u{i}" for i in range(11)]}
            )

    def test_short_name_rejected(self):
        with pytest.raises(ValidationError):
            CreateDishRouteInput.model_validate(
                {"name": "x", "dish_ids": ["a", "b"]}
            )


class TestCreateDishRouteHandler:
    @pytest.fixture
    def tool_anon(self):
        return make_create_dish_route_tool(
            AsyncMock(), user_id=None, conversation_id=None
        )

    async def test_anon_user_rejected(self, tool_anon):
        result = await tool_anon.handler(
            {"name": "Mi ruta", "dish_ids": ["u1", "u2"]}
        )
        assert "error" in result
        assert "log in" in result["error"].lower()


# ──────────────────────────────────────────────────────────────────────────
#   RequestReservationInput
# ──────────────────────────────────────────────────────────────────────────


class TestRequestReservationInput:
    def test_happy_path(self):
        inputs = RequestReservationInput.model_validate(
            {
                "restaurant_id": "11111111-1111-1111-1111-111111111111",
                "party_size": 4,
                "requested_for": "2026-12-31T21:00:00-03:00",
            }
        )
        assert inputs.party_size == 4

    def test_party_size_zero_rejected(self):
        with pytest.raises(ValidationError):
            RequestReservationInput.model_validate(
                {
                    "restaurant_id": "x",
                    "party_size": 0,
                    "requested_for": "2026-12-31T21:00:00-03:00",
                }
            )

    def test_party_size_above_thirty_rejected(self):
        with pytest.raises(ValidationError):
            RequestReservationInput.model_validate(
                {
                    "restaurant_id": "x",
                    "party_size": 50,
                    "requested_for": "2026-12-31T21:00:00-03:00",
                }
            )

    def test_message_too_long_rejected(self):
        with pytest.raises(ValidationError):
            RequestReservationInput.model_validate(
                {
                    "restaurant_id": "x",
                    "party_size": 2,
                    "requested_for": "2026-12-31T21:00:00-03:00",
                    "message": "x" * 700,
                }
            )


class TestRequestReservationHandler:
    @pytest.fixture
    def tool_anon(self):
        return make_request_reservation_tool(
            AsyncMock(), user_id=None, conversation_id=None
        )

    async def test_anon_user_rejected(self, tool_anon):
        result = await tool_anon.handler(
            {
                "restaurant_id": "11111111-1111-1111-1111-111111111111",
                "party_size": 2,
                "requested_for": "2026-12-31T21:00:00-03:00",
            }
        )
        assert "error" in result


# ──────────────────────────────────────────────────────────────────────────
#   ListRestaurantReviewsInput — schema pin
# ──────────────────────────────────────────────────────────────────────────


class TestListRestaurantReviewsInput:
    """Schema pin for the Sommelier review-listing tool.

    Pin the contract at the JSONSchema level so a refactor that
    silently widens / narrows the enum, the bounds, or the
    ``extra=forbid`` posture trips a unit test BEFORE the model sees
    it in production. The handler-level behaviour lives in
    ``test_list_restaurant_reviews_tool.py``; this file only checks
    the wire shape the LLM is given.
    """

    @pytest.fixture
    def tool(self):
        return make_list_restaurant_reviews_tool(AsyncMock(), user_id=None)

    def test_additional_properties_forbidden(self, tool):
        assert tool.input_schema.get("additionalProperties") is False

    def test_three_identifier_fields_present(self, tool):
        props = tool.input_schema["properties"]
        for key in ("restaurant_id", "restaurant_slug", "restaurant_name"):
            assert key in props, f"missing identifier field {key!r}"

    def test_sentiment_enum_pinned(self, tool):
        prop = tool.input_schema["properties"]["sentiment"]
        assert prop["enum"] == ["any", "positive", "neutral", "negative"]

    def test_sort_enum_pinned_includes_most_negative_and_most_positive(self, tool):
        prop = tool.input_schema["properties"]["sort"]
        # Set comparison — order is irrelevant at the wire level but the
        # full vocabulary is load-bearing for the LLM's NL → enum mapping.
        assert set(prop["enum"]) == {
            "recent",
            "oldest",
            "rating_high",
            "rating_low",
            "most_negative",
            "most_positive",
        }

    def test_rating_bounds_one_to_five(self, tool):
        # Nullable fields render under ``anyOf``; pull the numeric branch.
        for key in ("min_rating", "max_rating"):
            prop = tool.input_schema["properties"][key]
            numeric_branch = next(
                b for b in prop["anyOf"] if b.get("type") == "number"
            )
            assert numeric_branch.get("minimum") == 1
            assert numeric_branch.get("maximum") == 5

    def test_limit_bounds_one_to_fifty_default_ten(self, tool):
        prop = tool.input_schema["properties"]["limit"]
        assert prop["minimum"] == 1
        assert prop["maximum"] == 50
        assert prop.get("default") == 10

    def test_restaurant_name_min_length_two(self, tool):
        prop = tool.input_schema["properties"]["restaurant_name"]
        string_branch = next(
            b for b in prop["anyOf"] if b.get("type") == "string"
        )
        assert string_branch.get("minLength") == 2

    def test_no_responded_status_field(self, tool):
        # Owner-only concept — must NOT leak into the Sommelier contract.
        # If this regresses, the LLM might call the tool with a B2B-shaped
        # payload and the comensal output would lose its anonymous posture.
        assert "responded_status" not in tool.input_schema["properties"]
        assert "has_owner_response" not in tool.input_schema["properties"]

    def test_dates_render_as_iso_strings(self, tool):
        # ``date_from`` / ``date_to`` must serialise to ISO YYYY-MM-DD
        # so the LLM knows the wire format. Nullable → anyOf with the
        # date branch carrying ``format: "date"``.
        for key in ("date_from", "date_to"):
            prop = tool.input_schema["properties"][key]
            date_branch = next(
                b for b in prop["anyOf"] if b.get("format") == "date"
            )
            assert date_branch["type"] == "string"

    def test_extra_property_rejected_via_pydantic(self):
        # Same defense the JSONSchema declares — confirm Pydantic also
        # enforces it (the ``extra=forbid`` config). Belt + suspenders
        # in case the schema generator drifts.
        with pytest.raises(ValidationError):
            ListRestaurantReviewsInput.model_validate(
                {"restaurant_slug": "x", "rogue_field": "y"}
            )

    def test_model_validator_requires_an_identifier(self):
        with pytest.raises(ValidationError):
            ListRestaurantReviewsInput.model_validate({})
