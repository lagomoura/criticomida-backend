"""Integration tests for the enriched /api/social/dishes/{id}* endpoints.

Covers the new enrichment surface for the dish detail page (página estrella v2):
- GET /api/social/dishes/{id} (enriched detail)
- GET /api/social/dishes/{id}/aggregates
- GET /api/social/dishes/{id}/photos
- GET /api/social/dishes/{id}/diary-stats
- GET /api/social/dishes/{id}/related
- GET /api/social/dishes/{id}/editorial-blurb (204 when no blurb)
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


async def _create_review(
    client,
    cookies,
    *,
    place_id,
    restaurant_name,
    dish_name,
    score,
    text,
    city="Buenos Aires",
):
    payload = {
        "restaurant": {
            "place_id": place_id,
            "name": restaurant_name,
            "formatted_address": f"{restaurant_name}, {city}",
            "city": city,
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


async def _find_dish_id(client, restaurant_name: str, dish_name: str) -> str:
    r = await client.get(
        "/api/restaurants", params={"search": restaurant_name, "per_page": 5}
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, f"restaurant {restaurant_name!r} not found"
    slug = items[0]["slug"]
    rd = await client.get(f"/api/restaurants/{slug}/dishes")
    assert rd.status_code == 200, rd.text
    for d in rd.json():
        if d["name"] == dish_name:
            return d["id"]
    raise AssertionError(f"dish {dish_name!r} not found at {slug}")


@pytest.mark.asyncio
async def test_dish_detail_enriched_404(async_client_integration):
    bogus = str(uuid.uuid4())
    r = await async_client_integration.get(f"/api/social/dishes/{bogus}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dish_detail_enriched_returns_restaurant_context(
    async_client_integration, user_a
):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Dish Detail Test {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato {uuid.uuid4().hex[:4]}"

    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name=dish_name,
        score=4.5,
        text="Estaba muy bueno.",
    )
    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)

    r = await async_client_integration.get(f"/api/social/dishes/{dish_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == dish_name
    assert body["restaurant_name"] == rest_name
    assert body["restaurant_location_name"] is not None
    assert "is_signature" in body
    assert body["review_count"] >= 1
    assert "average_score" in body


@pytest.mark.asyncio
async def test_dish_aggregates_structure(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Aggr Test {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato {uuid.uuid4().hex[:4]}"

    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name=dish_name,
        score=5.0,
        text="Increíble.",
    )
    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)

    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/aggregates"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {
        "pros_top",
        "cons_top",
        "tags_top",
        "rating_histogram",
        "portion_distribution",
        "would_order_again",
        "photos_count",
        "unique_eaters",
    }
    assert isinstance(body["rating_histogram"], dict)
    assert set(body["rating_histogram"].keys()) == {"1", "2", "3", "4", "5"}
    assert body["unique_eaters"] >= 1


@pytest.mark.asyncio
async def test_dish_diary_stats(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Diary Dish {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato {uuid.uuid4().hex[:4]}"

    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name=dish_name,
        score=3.5,
        text="OK.",
    )
    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)

    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/diary-stats"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unique_eaters"] >= 1
    assert body["reviews_total"] >= 1
    assert body["reviews_last_7d"] >= 1
    assert isinstance(body["recent_eaters"], list)


@pytest.mark.asyncio
async def test_dish_photos_empty(async_client_integration, user_a):
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Photos Dish {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato {uuid.uuid4().hex[:4]}"
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name=dish_name,
        score=4.0,
        text="Sin foto.",
    )
    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)

    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/photos"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


@pytest.mark.asyncio
async def test_dish_related_excludes_self_restaurant(
    async_client_integration, user_a
):
    token = uuid.uuid4().hex[:6]
    dish_name = f"Milanesa {token}"

    place_id_a = f"pytest_place_{uuid.uuid4().hex[:10]}"
    place_id_b = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_a = f"Resto A {token}"
    rest_b = f"Resto B {token}"

    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id_a,
        restaurant_name=rest_a,
        dish_name=dish_name,
        score=4.0,
        text="Resto A.",
    )
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id_b,
        restaurant_name=rest_b,
        dish_name=dish_name,
        score=4.5,
        text="Resto B.",
    )

    dish_id_a = await _find_dish_id(async_client_integration, rest_a, dish_name)

    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id_a}/related"
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # B should appear; A (self) should not.
    names = {(it["restaurant_name"], it["name"]) for it in items}
    assert (rest_b, dish_name) in names
    assert all(it["restaurant_name"] != rest_a for it in items)


@pytest.mark.asyncio
async def test_dish_editorial_blurb_204_without_key(
    async_client_integration, user_a, monkeypatch
):
    monkeypatch.delenv("EDITORIAL_API_KEY", raising=False)
    monkeypatch.delenv("CHAT_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Blurb Test {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato {uuid.uuid4().hex[:4]}"
    await _create_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        restaurant_name=rest_name,
        dish_name=dish_name,
        score=4.0,
        text="Sin blurb.",
    )
    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)

    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/editorial-blurb"
    )
    assert r.status_code == 204
