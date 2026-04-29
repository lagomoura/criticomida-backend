"""Integration tests for POST /api/posts (social compose)."""

import os
import uuid

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_create_with_place_id_path(async_client_integration, user_a):
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"
    r = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": {
                "place_id": place_id,
                "name": "Places Test",
                "formatted_address": "Av. Test 123, CABA",
                "city": "Buenos Aires",
                "latitude": -34.6,
                "longitude": -58.4,
            },
            "dish_name": "Plato Places",
            "score": 4.0,
            "text": "Review via place_id path.",
        },
        cookies=user_a.cookies,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["dish"]["name"] == "Plato Places"
    assert body["dish"]["restaurant_name"] == "Places Test"


@pytest.mark.asyncio
async def test_create_legacy_name_path_still_works(
    async_client_integration, user_a
):
    r = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant_name": f"Legacy Resto {uuid.uuid4().hex[:6]}",
            "dish_name": "Legacy Plato",
            "score": 3.5,
            "text": "Review via legacy free-text path.",
        },
        cookies=user_a.cookies,
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_missing_restaurant_source_422(async_client_integration, user_a):
    r = await async_client_integration.post(
        "/api/posts",
        json={
            "dish_name": "Orphan",
            "score": 4,
            "text": "no restaurant provided",
        },
        cookies=user_a.cookies,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_same_user_can_review_same_dish_multiple_times(
    async_client_integration, user_a
):
    """A user is allowed to publish multiple reviews of the same dish — they
    form the dish's timeline for that user. Both posts should succeed and
    target the same dish_id."""
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"
    payload = {
        "restaurant": {
            "place_id": place_id,
            "name": "Dup Test",
            "city": "Buenos Aires",
        },
        "dish_name": "DupDish",
        "score": 4,
        "text": "Primera review del mismo dish.",
    }
    first = await async_client_integration.post(
        "/api/posts", json=payload, cookies=user_a.cookies
    )
    assert first.status_code == 201
    second = await async_client_integration.post(
        "/api/posts",
        json={**payload, "text": "Segunda visita, sigue rico."},
        cookies=user_a.cookies,
    )
    assert second.status_code == 201
    assert first.json()["dish"]["id"] == second.json()["dish"]["id"]
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
async def test_dish_id_reuses_existing_dish(
    async_client_integration, user_a, user_b
):
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"
    first = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": {"place_id": place_id, "name": "Shared"},
            "dish_name": "SharedDish",
            "score": 4,
            "text": "first",
        },
        cookies=user_a.cookies,
    )
    dish_id = first.json()["dish"]["id"]

    second = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": {"place_id": place_id, "name": "Shared"},
            "dish_id": dish_id,
            "dish_name": "something-else-ignored",
            "score": 3.5,
            "text": "second user via dish_id",
        },
        cookies=user_b.cookies,
    )
    assert second.status_code == 201
    # Same underlying dish row, original name preserved.
    assert second.json()["dish"]["id"] == dish_id
    assert second.json()["dish"]["name"] == "SharedDish"


@pytest.mark.asyncio
async def test_dish_id_wrong_restaurant_400(
    async_client_integration, user_a
):
    # Create one dish under place_id A...
    place_a = f"pytest_{uuid.uuid4().hex[:10]}"
    created = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": {"place_id": place_a, "name": "A"},
            "dish_name": "A-dish",
            "score": 4,
            "text": "xxxxxxxxxxxxxxxxxxxxx",
        },
        cookies=user_a.cookies,
    )
    dish_id = created.json()["dish"]["id"]

    # ...then try to reuse that dish_id under a different restaurant.
    place_b = f"pytest_{uuid.uuid4().hex[:10]}"
    r = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": {"place_id": place_b, "name": "B"},
            "dish_id": dish_id,
            "dish_name": "anything",
            "score": 4,
            "text": "should fail",
        },
        cookies=user_a.cookies,
    )
    assert r.status_code == 400
