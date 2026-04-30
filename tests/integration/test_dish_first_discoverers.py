"""Integration tests for `first_discoverers` en GET /api/social/dishes/{id}.

Verifica que:
- El podio devuelve los 3 primeros reseñadores DISTINTOS, ordenados por
  `created_at` ASC.
- Los reseñadores anónimos (`is_anonymous=True`) quedan fuera del podio.
- Si un mismo usuario reseña dos veces el mismo plato, cuenta una sola vez
  (la reseña más temprana).
- Cada reseñador trae el shape esperado (rank, user_id, handle, display_name,
  avatar_url, discovered_at, review_id).
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
    score: float = 4.0,
    is_anonymous: bool = False,
) -> str:
    """POST /api/posts. Devuelve el review id."""
    payload: dict[str, Any] = {
        "restaurant": {
            "place_id": place_id,
            "name": restaurant_name,
            "formatted_address": f"{restaurant_name}, Buenos Aires",
            "city": "Buenos Aires",
            "latitude": -34.6,
            "longitude": -58.4,
        },
        "dish_name": dish_name,
        "score": score,
        "text": "Test discoverer review.",
    }
    if is_anonymous:
        payload["extras"] = {"is_anonymous": True}
    r = await client.post("/api/posts", json=payload, cookies=cookies)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _find_dish_id(
    client: httpx.AsyncClient, restaurant_name: str, dish_name: str
) -> str:
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
async def test_first_discoverers_returns_top_3(async_client_integration):
    """4 usuarios distintos reseñan el mismo plato — solo los 3 primeros van
    al podio, en orden de created_at ASC con ranks 1/2/3."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Discoverers {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Discoverer {uuid.uuid4().hex[:4]}"

    user1 = await register_and_login(async_client_integration)
    user2 = await register_and_login(async_client_integration)
    user3 = await register_and_login(async_client_integration)
    user4 = await register_and_login(async_client_integration)

    for u in (user1, user2, user3, user4):
        await _post_review(
            async_client_integration, u.cookies,
            place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(f"/api/social/dishes/{dish_id}")
    assert r.status_code == 200
    body = r.json()

    discoverers = body["first_discoverers"]
    assert len(discoverers) == 3
    assert [d["rank"] for d in discoverers] == [1, 2, 3]
    assert [d["user_id"] for d in discoverers] == [
        user1.user_id, user2.user_id, user3.user_id,
    ]
    # user4 quedó afuera del podio.
    assert user4.user_id not in {d["user_id"] for d in discoverers}


@pytest.mark.asyncio
async def test_first_discoverers_excludes_anonymous(async_client_integration):
    """Las reseñas anónimas no entran al podio aunque sean cronológicamente
    primeras — pierde la narrativa "quién llegó primero" si el primero es
    anónimo."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Anon {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Anon {uuid.uuid4().hex[:4]}"

    anon_user = await register_and_login(async_client_integration)
    visible_user = await register_and_login(async_client_integration)

    # El anónimo escribe primero; el visible después.
    await _post_review(
        async_client_integration, anon_user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        is_anonymous=True,
    )
    await _post_review(
        async_client_integration, visible_user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(f"/api/social/dishes/{dish_id}")
    discoverers = r.json()["first_discoverers"]

    # Solo el visible aparece, con rank 1.
    assert len(discoverers) == 1
    assert discoverers[0]["user_id"] == visible_user.user_id
    assert discoverers[0]["rank"] == 1


@pytest.mark.asyncio
async def test_first_discoverers_dedup_same_user(async_client_integration):
    """Si user_a reseña 2 veces el mismo plato antes que user_b, su review más
    temprana cuenta como rank 1 y user_b queda como rank 2 — user_a NO ocupa
    los ranks 1 y 2 a la vez."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Dedup {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Dedup {uuid.uuid4().hex[:4]}"

    user_a = await register_and_login(async_client_integration)
    user_b = await register_and_login(async_client_integration)

    # user_a reseña dos veces.
    review_a1 = await _post_review(
        async_client_integration, user_a.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )
    await _post_review(
        async_client_integration, user_a.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )
    # user_b reseña después.
    await _post_review(
        async_client_integration, user_b.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(f"/api/social/dishes/{dish_id}")
    discoverers = r.json()["first_discoverers"]

    # Exactamente 2 cronistas distintos: user_a (rank 1, primer review) y user_b (rank 2).
    assert len(discoverers) == 2
    assert discoverers[0]["rank"] == 1
    assert discoverers[0]["user_id"] == user_a.user_id
    # La reseña destacada para user_a es la más temprana (la primera que posteó).
    assert discoverers[0]["review_id"] == review_a1
    assert discoverers[1]["rank"] == 2
    assert discoverers[1]["user_id"] == user_b.user_id


@pytest.mark.asyncio
async def test_first_discoverers_empty_when_no_visible_reviews(
    async_client_integration,
):
    """Plato con solo reseñas anónimas → first_discoverers viene vacío."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Solo Anon {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Solo Anon {uuid.uuid4().hex[:4]}"

    user = await register_and_login(async_client_integration)
    await _post_review(
        async_client_integration, user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        is_anonymous=True,
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(f"/api/social/dishes/{dish_id}")
    assert r.json()["first_discoverers"] == []


@pytest.mark.asyncio
async def test_first_discoverers_payload_shape(async_client_integration):
    """Cada entrada del podio trae el shape esperado por el frontend."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Shape {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Shape {uuid.uuid4().hex[:4]}"

    user = await register_and_login(async_client_integration)
    review_id = await _post_review(
        async_client_integration, user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
    )
    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(f"/api/social/dishes/{dish_id}")
    discoverers = r.json()["first_discoverers"]

    assert len(discoverers) == 1
    d = discoverers[0]
    expected_keys = {
        "rank", "user_id", "handle", "display_name", "avatar_url",
        "discovered_at", "review_id",
    }
    assert expected_keys.issubset(d.keys())
    assert d["rank"] == 1
    assert d["user_id"] == user.user_id
    assert d["review_id"] == review_id
