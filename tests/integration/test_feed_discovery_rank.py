"""Integration tests for `discovery_rank` en cada FeedItem.

`discovery_rank` (1|2|3|null) marca la posición del autor entre los primeros
3 reseñadores DISTINTOS del plato. Si un usuario reseña dos veces el mismo
plato, solo su reseña más temprana lleva rank — la otra queda en null.

Verificamos vía GET /api/social/dishes/{id}/reviews (que usa el mismo
_build_feed_items que el feed principal).
"""

import os
import uuid
from typing import Any

import httpx
import pytest

from tests.integration.conftest import register_and_login

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _post_review(
    client: httpx.AsyncClient,
    cookies: Any,
    *,
    place_id: str,
    restaurant_name: str,
    dish_name: str,
) -> str:
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
        "score": 4.0,
        "text": "Test discovery rank.",
    }
    r = await client.post("/api/posts", json=payload, cookies=cookies)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _find_dish_id(
    client: httpx.AsyncClient, restaurant_name: str, dish_name: str
) -> str:
    r = await client.get(
        "/api/restaurants", params={"search": restaurant_name, "per_page": 5}
    )
    items = r.json()["items"]
    assert items, f"restaurant {restaurant_name!r} not found"
    slug = items[0]["slug"]
    rd = await client.get(f"/api/restaurants/{slug}/dishes")
    for d in rd.json():
        if d["name"] == dish_name:
            return d["id"]
    raise AssertionError(f"dish {dish_name!r} not found at {slug}")


@pytest.mark.asyncio
async def test_discovery_rank_first_3_users_get_1_2_3(async_client_integration):
    """4 usuarios reseñan el mismo plato — los 3 primeros llevan rank 1/2/3,
    el 4to lleva discovery_rank=null."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Rank {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Rank {uuid.uuid4().hex[:4]}"

    users = [await register_and_login(async_client_integration) for _ in range(4)]
    review_ids: list[str] = []
    for u in users:
        rid = await _post_review(
            async_client_integration, u.cookies,
            place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        )
        review_ids.append(rid)

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/reviews?limit=20"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 4

    rank_by_review = {it["id"]: it.get("discovery_rank") for it in items}
    assert rank_by_review[review_ids[0]] == 1
    assert rank_by_review[review_ids[1]] == 2
    assert rank_by_review[review_ids[2]] == 3
    assert rank_by_review[review_ids[3]] is None


@pytest.mark.asyncio
async def test_discovery_rank_dedup_same_user(async_client_integration):
    """Si user_a reseña dos veces antes que user_b, su PRIMERA reseña recibe
    rank 1 y la segunda discovery_rank=null. user_b queda como rank 2."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Rank Dedup {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Rank Dedup {uuid.uuid4().hex[:4]}"

    user_a = await register_and_login(async_client_integration)
    user_b = await register_and_login(async_client_integration)

    rev_a1 = await _post_review(
        async_client_integration, user_a.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )
    rev_a2 = await _post_review(
        async_client_integration, user_a.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )
    rev_b = await _post_review(
        async_client_integration, user_b.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/reviews?limit=20"
    )
    items = r.json()["items"]
    rank_by_review = {it["id"]: it.get("discovery_rank") for it in items}

    # La primera reseña de user_a se lleva rank 1; la segunda queda en null.
    assert rank_by_review[rev_a1] == 1
    assert rank_by_review[rev_a2] is None
    # user_b queda como rank 2 — el 2do CRONISTA distinto, no la 2da reseña.
    assert rank_by_review[rev_b] == 2


@pytest.mark.asyncio
async def test_discovery_rank_null_outside_top_3(async_client_integration):
    """Reviews del 4to autor en adelante llevan discovery_rank=null."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Rank Many {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Rank Many {uuid.uuid4().hex[:4]}"

    users = [await register_and_login(async_client_integration) for _ in range(5)]
    review_ids: list[str] = []
    for u in users:
        rid = await _post_review(
            async_client_integration, u.cookies,
            place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        )
        review_ids.append(rid)

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/reviews?limit=20"
    )
    items = r.json()["items"]
    rank_by_review = {it["id"]: it.get("discovery_rank") for it in items}

    # Solo los primeros 3 tienen rank.
    ranks_present = [r for r in rank_by_review.values() if r is not None]
    assert sorted(ranks_present) == [1, 2, 3]
