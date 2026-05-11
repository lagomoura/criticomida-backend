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


# =====================================================================
# Duelo de Platos — por raíz semántica + pilar elegido (migración 059)
# =====================================================================


async def _seed_root_duel(
    client: httpx.AsyncClient,
    cookies: Any,
    *,
    dish_name: str,
    restaurant_name: str,
    score: float,
    value_prop: int | None = None,
    execution: int | None = None,
    presentation: int | None = None,
) -> None:
    await _post_review_with_pillars(
        client,
        cookies,
        place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
        restaurant_name=restaurant_name,
        dish_name=dish_name,
        score=score,
        value_prop=value_prop,
        execution=execution,
        presentation=presentation,
    )


@pytest.mark.asyncio
async def test_duel_by_root_returns_two_distinct_restaurants(async_client_integration):
    """`?root=sorrentinos` enfrenta dos platos de restaurantes DIFERENTES."""
    user = await register_and_login(async_client_integration)
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Sorrentinos de jamón y queso",
        restaurant_name="Resto Root A", score=4.5, value_prop=3,
    )
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Sorrentinos al pomodoro",
        restaurant_name="Resto Root B", score=4.0, value_prop=2,
    )
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Sorrentinos rellenos",
        restaurant_name="Resto Root C", score=3.5, value_prop=1,
    )

    r = await async_client_integration.get(
        "/api/dishes/duel?root=sorrentinos&pillar=value_prop"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pillar"] == "value_prop"
    assert body["root"] == "sorrentinos"
    assert body["fallback_reason"] is None
    items = body["items"]
    assert len(items) == 2
    restaurant_ids = {it["restaurant_id"] for it in items}
    assert len(restaurant_ids) == 2, "Los 2 platos deben ser de restaurantes distintos"


@pytest.mark.asyncio
async def test_duel_pillar_value_prop_vs_execution_changes_order(async_client_integration):
    """Cambiar `pillar` reordena los contendientes."""
    user = await register_and_login(async_client_integration)
    # A: value_prop alto, execution bajo.
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Milanesa napolitana", restaurant_name="Resto Mila A",
        score=4.0, value_prop=3, execution=1,
    )
    # B: execution alto, value_prop bajo.
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Milanesa con papas", restaurant_name="Resto Mila B",
        score=4.0, value_prop=1, execution=3,
    )

    r_val = await async_client_integration.get(
        "/api/dishes/duel?root=milanesa&pillar=value_prop"
    )
    r_exec = await async_client_integration.get(
        "/api/dishes/duel?root=milanesa&pillar=execution"
    )
    assert r_val.status_code == 200 and r_exec.status_code == 200
    val_items = r_val.json()["items"]
    exec_items = r_exec.json()["items"]
    assert len(val_items) == 2 and len(exec_items) == 2
    # Ganador value_prop = restaurante A; ganador execution = restaurante B.
    assert val_items[0]["restaurant_name"] == "Resto Mila A"
    assert exec_items[0]["restaurant_name"] == "Resto Mila B"


@pytest.mark.asyncio
async def test_duel_pillar_overall_rating(async_client_integration):
    """`pillar=overall_rating` ordena por stars."""
    user = await register_and_login(async_client_integration)
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Pizza fugazzeta",
        restaurant_name="Resto Pizza High", score=5.0, value_prop=2,
    )
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Pizza margarita",
        restaurant_name="Resto Pizza Low", score=2.0, value_prop=2,
    )

    r = await async_client_integration.get(
        "/api/dishes/duel?root=pizza&pillar=overall_rating"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["restaurant_name"] == "Resto Pizza High"


@pytest.mark.asyncio
async def test_duel_root_unique_restaurant_returns_fallback(async_client_integration):
    """Con un solo restaurante para la raíz, items=[] + fallback_reason."""
    # Sentinel inventado para no colisionar con seeds ni con otros tests del run.
    sentinel = f"zzzpytuniq{uuid.uuid4().hex[:8]}"
    user = await register_and_login(async_client_integration)
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name=sentinel,
        restaurant_name="Resto Único Sentinel", score=4.5, value_prop=3,
    )

    r = await async_client_integration.get(
        f"/api/dishes/duel?root={sentinel}&pillar=value_prop"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["fallback_reason"] == "root_unique_restaurant"


@pytest.mark.asyncio
async def test_duel_root_not_found_returns_fallback(async_client_integration):
    """Raíz inexistente → items=[] + fallback_reason='root_not_found'."""
    r = await async_client_integration.get(
        "/api/dishes/duel?root=inventoinventado12345&pillar=value_prop"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["fallback_reason"] == "root_not_found"


@pytest.mark.asyncio
async def test_duel_legacy_category_only(async_client_integration):
    """Cliente viejo: sólo `category` (sin `root`) sigue funcionando."""
    user = await register_and_login(async_client_integration)
    await _post_review_with_pillars(
        async_client_integration, user.cookies,
        place_id=f"pytest_place_{uuid.uuid4().hex[:8]}",
        restaurant_name="Resto Legacy 1", dish_name="Plato L1",
        score=4.5, value_prop=3,
    )
    cats = await async_client_integration.get("/api/categories")
    if cats.status_code != 200 or not cats.json():
        pytest.skip("No categories seeded")
    slug = cats.json()[0]["slug"]

    r = await async_client_integration.get(f"/api/dishes/duel?category={slug}")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


@pytest.mark.asyncio
async def test_duel_normalizes_input_root(async_client_integration):
    """El handler normaliza el root del cliente (mayúsculas/acentos)."""
    user = await register_and_login(async_client_integration)
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Empanadas de carne",
        restaurant_name="Resto Empa 1", score=4.0, value_prop=3,
    )
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Empanadas de jamón y queso",
        restaurant_name="Resto Empa 2", score=4.0, value_prop=2,
    )

    # "EMPANADAS" en mayúsculas debe matchear "empanadas".
    r = await async_client_integration.get(
        "/api/dishes/duel?root=EMPANADAS&pillar=value_prop"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2


@pytest.mark.asyncio
async def test_duel_roots_endpoint_lists_popular_roots(async_client_integration):
    """`/duel/roots` lista raíces con >= min_restaurants contendientes."""
    user = await register_and_login(async_client_integration)
    # Usamos sentinels para aislar de seeds y de otros tests que dejen state.
    popular = f"zzzpytpop{uuid.uuid4().hex[:8]}"
    solo = f"zzzpytsolo{uuid.uuid4().hex[:8]}"

    # 2 restaurantes con la raíz popular → debería listarse.
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name=f"{popular} con cheddar",
        restaurant_name="Resto Pop A", score=4.0, value_prop=3,
    )
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name=f"{popular} doble",
        restaurant_name="Resto Pop B", score=4.5, value_prop=2,
    )
    # 1 solo restaurante con la raíz "solo" → NO debería listarse.
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name=f"{solo} casera",
        restaurant_name="Resto Solo X", score=4.0, value_prop=2,
    )

    r = await async_client_integration.get(
        "/api/dishes/duel/roots?limit=50&min_restaurants=2"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    roots = {it["root"] for it in body["items"]}
    assert popular in roots
    assert solo not in roots


@pytest.mark.asyncio
async def test_dish_root_extract_strips_stopwords_and_parens(async_client_integration):
    """Sanity check de la heurística end-to-end: paréntesis al inicio y
    stopwords se descartan. Dos restaurantes con "(Especial) Pollo" y
    "Pollo de campo" comparten root='pollo' y aparecen en el duelo.
    """
    user = await register_and_login(async_client_integration)
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="(Especial) Pollo al horno",
        restaurant_name="Heur Pollo A", score=4.0, value_prop=3,
    )
    await _seed_root_duel(
        async_client_integration, user.cookies,
        dish_name="Pollo de campo",
        restaurant_name="Heur Pollo B", score=4.0, value_prop=2,
    )
    r = await async_client_integration.get(
        "/api/dishes/duel?root=pollo&pillar=value_prop"
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 2
