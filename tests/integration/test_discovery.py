"""Integration tests for the discovery feed (Geek Score, duel)."""

import os
import uuid
from typing import Any

import httpx
import pytest

from tests.integration.conftest import create_review, register_and_login

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _post_review_with_pillars(
    client: httpx.AsyncClient,
    cookies: Any,
    *,
    place_id: str,
    restaurant_name: str,
    dish_name: str,
    score: float,
    presentation: int | None = None,
    value_prop: int | None = None,
    execution: int | None = None,
    lat: float = -34.6,
    lng: float = -58.4,
) -> str:
    extras: dict[str, Any] = {}
    if presentation is not None:
        extras["presentation"] = presentation
    if value_prop is not None:
        extras["value_prop"] = value_prop
    if execution is not None:
        extras["execution"] = execution

    payload: dict[str, Any] = {
        "restaurant": {
            "place_id": place_id,
            "name": restaurant_name,
            "formatted_address": f"{restaurant_name}, BA",
            "city": "Buenos Aires",
            "latitude": lat,
            "longitude": lng,
        },
        "dish_name": dish_name,
        "score": score,
        "text": f"Pilares review: pres={presentation} val={value_prop} exec={execution}",
    }
    if extras:
        payload["extras"] = extras
    r = await client.post("/api/posts", json=payload, cookies=cookies)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_discover_returns_dishes_sorted_by_geek_score(async_client_integration):
    """3 platos en CABA con perfiles de pilares distintos.

    Plato A: 1 review 5.0★ pero ejecución 1 (mala técnica) → debería rankear
             bajo (Bayesian shrink + execution_norm bajo).
    Plato B: 1 review 4.0★ con ejecución 3 (perfecta) → debería rankear arriba.
    """
    user = await register_and_login(async_client_integration)

    a_pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    b_pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=a_pid,
        restaurant_name="Resto Mala Técnica",
        dish_name="Plato A",
        score=5.0,
        presentation=2,
        value_prop=2,
        execution=1,
    )
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=b_pid,
        restaurant_name="Resto Buena Técnica",
        dish_name="Plato B",
        score=4.0,
        presentation=2,
        value_prop=2,
        execution=3,
    )

    r = await async_client_integration.get(
        "/api/dishes/discover?sort=geek_score&limit=50"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    a = next((it for it in items if it["dish_name"] == "Plato A"), None)
    b = next((it for it in items if it["dish_name"] == "Plato B"), None)
    assert a is not None and b is not None
    assert b["geek_score"] > a["geek_score"], (
        f"B (mejor execution) debería estar por encima de A. "
        f"A={a['geek_score']} B={b['geek_score']}"
    )


@pytest.mark.asyncio
async def test_discover_radius_filters_far_dishes(async_client_integration):
    """Radio Haversine excluye platos fuera del círculo."""
    user = await register_and_login(async_client_integration)
    near_pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    far_pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=near_pid,
        restaurant_name="Resto Cerca",
        dish_name="Plato Cerca",
        score=4.5,
        execution=3,
        lat=-34.6,
        lng=-58.4,
    )
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=far_pid,
        restaurant_name="Resto Lejos",
        dish_name="Plato Lejos",
        score=4.5,
        execution=3,
        lat=-31.4,
        lng=-64.2,  # Córdoba, ~600km
    )

    r = await async_client_integration.get(
        "/api/dishes/discover?lat=-34.6&lng=-58.4&radius_km=20&limit=50"
    )
    assert r.status_code == 200
    names = {it["dish_name"] for it in r.json()["items"]}
    assert "Plato Cerca" in names
    assert "Plato Lejos" not in names


@pytest.mark.asyncio
async def test_duel_returns_at_most_two(async_client_integration):
    """El duelo nunca devuelve más de 2 platos."""
    user = await register_and_login(async_client_integration)
    for i in range(3):
        await _post_review_with_pillars(
            async_client_integration,
            user.cookies,
            place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
            restaurant_name=f"Resto Duel {i}",
            dish_name=f"Plato Duel {i}",
            score=4.5,
            value_prop=3 - (i % 3),  # 3, 2, 1
        )

    # Necesitamos un slug de categoría real para este endpoint. Usamos cualquiera
    # que devuelva /api/categories.
    cats = await async_client_integration.get("/api/categories")
    if cats.status_code != 200 or not cats.json():
        pytest.skip("No categories seeded; cannot test duel endpoint")
    slug = cats.json()[0]["slug"]

    r = await async_client_integration.get(f"/api/dishes/duel?category={slug}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) <= 2
