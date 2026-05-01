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
async def test_nearby_smart_combines_proximity_and_execution(async_client_integration):
    """nearby_smart prioriza cercanía + ejecución técnica.

    Tres platos:
      A: cerca (0km), execution=3 → debería rankear primero (alta prox + alta exec).
      C: lejos (~12km), execution=3 → menor que A por la penalización de proximidad,
         pero mayor que D porque la ejecución pesa más.
      D: cerca (0km), execution=1 → ejecución mala penaliza más que la cercanía buena.

    Verifica orden: A > C > D.

    No testeamos recency directamente porque las 3 reviews son "nuevas" en este test
    (created_at = now). El score recency aporta lo mismo a las 3, así que solo
    validamos el efecto compuesto de proximidad + ejecución.
    """
    user = await register_and_login(async_client_integration)
    base_lat, base_lng = -34.6, -58.4

    # A: misma coord que la query → distance ≈ 0km
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
        restaurant_name="Resto A Cerca Tecnico",
        dish_name="Plato A nearby_smart",
        score=4.0,
        execution=3,
        lat=base_lat,
        lng=base_lng,
    )
    # C: ~12km al norte → cae en bucket [5, 15]km
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
        restaurant_name="Resto C Lejos Tecnico",
        dish_name="Plato C nearby_smart",
        score=4.0,
        execution=3,
        lat=base_lat + 0.108,  # ≈ 12km en latitud
        lng=base_lng,
    )
    # D: misma coord que la query, ejecución pobre
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
        restaurant_name="Resto D Cerca Mala",
        dish_name="Plato D nearby_smart",
        score=4.0,
        execution=1,
        lat=base_lat,
        lng=base_lng,
    )

    r = await async_client_integration.get(
        f"/api/dishes/discover?lat={base_lat}&lng={base_lng}"
        f"&radius_km=50&sort=nearby_smart&limit=50"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    names_in_order = [it["dish_name"] for it in items]

    a_idx = names_in_order.index("Plato A nearby_smart")
    c_idx = names_in_order.index("Plato C nearby_smart")
    d_idx = names_in_order.index("Plato D nearby_smart")

    assert a_idx < c_idx, (
        f"A (cerca + exec3) debe rankear antes que C (lejos + exec3). "
        f"Orden actual: {names_in_order}"
    )
    assert c_idx < d_idx, (
        f"C (lejos + exec3) debe rankear antes que D (cerca + exec1) — "
        f"la ejecución técnica pesa más que la cercanía. "
        f"Orden actual: {names_in_order}"
    )


@pytest.mark.asyncio
async def test_nearby_smart_falls_back_when_no_geo(async_client_integration):
    """Sin lat/lng, nearby_smart debe degradar a geek_score sin reventar."""
    user = await register_and_login(async_client_integration)
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
        restaurant_name="Resto Fallback",
        dish_name="Plato Fallback",
        score=4.0,
        execution=3,
    )

    r = await async_client_integration.get(
        "/api/dishes/discover?sort=nearby_smart&limit=10"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["dish_name"] == "Plato Fallback" for it in items)


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
