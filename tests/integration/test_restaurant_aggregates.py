"""Integration tests for restaurant aggregation endpoints.

Covers:
- GET /api/restaurants/{slug}/aggregates
- GET /api/restaurants/{slug}/photos
- GET /api/restaurants/{slug}/diary-stats
- GET /api/restaurants/{slug}/signature-dishes
"""

import os
import uuid

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _create_review(client, cookies, *, place_id, restaurant_name, dish_name, score, text):
    payload = {
        "restaurant": {
            "place_id": place_id,
            "name": restaurant_name,
            "formatted_address": f"{restaurant_name}, BA",
            "city": "Buenos Aires",
            "latitude": -34.6,
            "longitude": -58.4,
        },
        "dish_name": dish_name,
        "score": score,
        "text": text,
    }
    r = await client.post("/api/posts", json=payload, cookies=cookies)
    assert r.status_code == 201, r.text
    return r.json()


async def _find_slug_by_name(client, name: str) -> str:
    r = await client.get("/api/restaurants", params={"search": name, "per_page": 5})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, f"restaurant not found by search={name!r}"
    return items[0]["slug"]


@pytest.mark.asyncio
async def test_aggregates_404_unknown_slug(async_client_integration):
    r = await async_client_integration.get(
        "/api/restaurants/does-not-exist-xyz/aggregates"
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_aggregates_returns_structure(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Aggregates Test {uuid.uuid4().hex[:6]}"
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name="Plato A",
        score=4.5,
        text="Estaba muy bueno.",
    )
    slug = await _find_slug_by_name(async_client_integration, rest_name)

    r = await async_client_integration.get(f"/api/restaurants/{slug}/aggregates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pros_top" in body
    assert "cons_top" in body
    assert "dimension_averages" in body
    assert body["dishes_count"] >= 1
    assert body["reviews_count"] >= 1
    assert isinstance(body["pros_top"], list)
    assert isinstance(body["cons_top"], list)


@pytest.mark.asyncio
async def test_photos_empty_when_no_review_images(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Photos Test {uuid.uuid4().hex[:6]}"
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name="Plato sin foto",
        score=3.0,
        text="Reseña sin imagen.",
    )
    slug = await _find_slug_by_name(async_client_integration, rest_name)

    r = await async_client_integration.get(f"/api/restaurants/{slug}/photos")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_diary_stats_zero_visits_initially(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Diary Test {uuid.uuid4().hex[:6]}"
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name="Plato Diary",
        score=4.0,
        text="OK.",
    )
    slug = await _find_slug_by_name(async_client_integration, rest_name)

    r = await async_client_integration.get(f"/api/restaurants/{slug}/diary-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["unique_visitors"] == 0
    assert body["visits_total"] == 0
    assert body["visits_last_7d"] == 0
    assert body["recent_visitors"] == []
    # most_ordered_dish derives from reviews, not diary; the seeded review
    # should make this non-null.
    assert body["most_ordered_dish"] is not None
    assert body["most_ordered_dish"]["name"] == "Plato Diary"


@pytest.mark.asyncio
async def test_signature_dishes_returns_top(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Signature Test {uuid.uuid4().hex[:6]}"
    # Two dishes; one rated 5, one rated 2 — 5 wins signature spot.
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name="Plato top",
        score=5.0,
        text="Excelente.",
    )
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name="Plato meh",
        score=2.0,
        text="Regular.",
    )
    slug = await _find_slug_by_name(async_client_integration, rest_name)

    r = await async_client_integration.get(
        f"/api/restaurants/{slug}/signature-dishes?limit=1"
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "Plato top"
    assert body["items"][0]["best_quote"] is not None
