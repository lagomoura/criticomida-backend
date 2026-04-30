"""Integration tests for the map BBOX endpoint (Discovery Mode tab Mapa)."""

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


# Bbox CABA centro: lat -34.7..-34.5, lng -58.5..-58.3
BBOX_PARAMS = "min_lat=-34.7&min_lng=-58.5&max_lat=-34.5&max_lng=-58.3"


@pytest.mark.asyncio
async def test_bbox_includes_inside_excludes_outside(async_client_integration):
    """Restaurantes fuera del bbox no aparecen; los de adentro sí."""
    user = await register_and_login(async_client_integration)
    inside_pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    outside_pid = f"pytest_place_{uuid.uuid4().hex[:8]}"

    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=inside_pid,
        restaurant_name="Resto Adentro",
        dish_name="Plato Adentro",
        score=4.5,
        execution=3,
        value_prop=2,
        presentation=2,
        lat=-34.6,
        lng=-58.4,
    )
    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=outside_pid,
        restaurant_name="Resto Cordoba",
        dish_name="Plato Cordoba",
        score=4.5,
        execution=3,
        lat=-31.4,
        lng=-64.2,
    )

    r = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r.status_code == 200, r.text
    data = r.json()
    names = {item["name"] for item in data["items"]}
    assert "Resto Adentro" in names
    assert "Resto Cordoba" not in names
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_bbox_excludes_restaurants_without_reviews(async_client_integration):
    """Un local sin platos reseñados no aparece (INNER JOIN a dish_reviews)."""
    user = await register_and_login(async_client_integration)
    pid_with_review = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_no_review = f"pytest_place_{uuid.uuid4().hex[:8]}"

    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=pid_with_review,
        restaurant_name="Resto Con Review",
        dish_name="Plato",
        score=4.0,
        execution=2,
    )

    create_payload = {
        "google_place_id": pid_no_review,
        "name": "Resto Sin Reviews",
        "city": "Buenos Aires",
        "latitude": -34.6,
        "longitude": -58.4,
    }
    create_resp = await async_client_integration.post(
        "/api/restaurants", json=create_payload, cookies=user.cookies
    )
    if create_resp.status_code in (401, 403):
        pytest.skip("Restaurant create requires admin/critic role")
    assert create_resp.status_code in (200, 201), create_resp.text

    r = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r.status_code == 200
    names = {item["name"] for item in r.json()["items"]}
    assert "Resto Con Review" in names
    assert "Resto Sin Reviews" not in names


@pytest.mark.asyncio
async def test_chef_badge_requires_min_reviews(async_client_integration):
    """1 review con execution=3 NO da chef badge (shrinkage protege de 1-review wonders)."""
    user = await register_and_login(async_client_integration)
    pid = f"pytest_place_{uuid.uuid4().hex[:8]}"

    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=pid,
        restaurant_name="Resto Una Review",
        dish_name="Plato",
        score=5.0,
        execution=3,
        value_prop=3,
    )

    r = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r.status_code == 200
    pin = next(
        (it for it in r.json()["items"] if it["name"] == "Resto Una Review"), None
    )
    assert pin is not None
    assert pin["has_chef_badge"] is False
    assert pin["has_gem_badge"] is False


@pytest.mark.asyncio
async def test_chef_and_gem_badges_with_enough_reviews(async_client_integration):
    """Con varios reviews execution=3 (y value_prop=3) los badges se activan."""
    pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    for _ in range(5):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid,
            restaurant_name="Resto Top",
            dish_name="Plato Estrella",
            score=5.0,
            execution=3,
            value_prop=3,
            presentation=3,
        )

    r = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r.status_code == 200
    pin = next((it for it in r.json()["items"] if it["name"] == "Resto Top"), None)
    assert pin is not None
    assert pin["has_chef_badge"] is True
    assert pin["has_gem_badge"] is True
    assert pin["golden_dish"] is not None
    assert pin["best_value_dish"] is not None
    assert pin["golden_dish"]["name"] == "Plato Estrella"


@pytest.mark.asyncio
async def test_golden_and_best_value_pick_different_dishes(async_client_integration):
    """Cuando hay 2 platos: el de mejor execution = Golden, el de mejor value = Best Value."""
    pid = f"pytest_place_{uuid.uuid4().hex[:8]}"
    # 4 reviews del plato 'Tecnico' con execution=3 y value=1
    for _ in range(4):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid,
            restaurant_name="Resto Mix",
            dish_name="Plato Tecnico",
            score=4.5,
            execution=3,
            value_prop=1,
        )
    # 4 reviews del plato 'Ganga' con value=3 y execution=1
    for _ in range(4):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid,
            restaurant_name="Resto Mix",
            dish_name="Plato Ganga",
            score=4.0,
            execution=1,
            value_prop=3,
        )

    r = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r.status_code == 200
    pin = next((it for it in r.json()["items"] if it["name"] == "Resto Mix"), None)
    assert pin is not None
    assert pin["golden_dish"]["name"] == "Plato Tecnico"
    assert pin["best_value_dish"]["name"] == "Plato Ganga"


@pytest.mark.asyncio
async def test_bbox_validates_inverted_bounds(async_client_integration):
    r = await async_client_integration.get(
        "/api/restaurants/in-bbox?min_lat=10&min_lng=10&max_lat=5&max_lng=20"
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_bbox_sort_by_value_prop(async_client_integration):
    """sort=value_prop debe rankear primero el local con mejor C/B."""
    pid_high_value = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_high_exec = f"pytest_place_{uuid.uuid4().hex[:8]}"
    # 4 reviews del local "Ganga": value=3, execution=1
    for _ in range(4):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid_high_value,
            restaurant_name="Resto Ganga",
            dish_name="Plato Barato",
            score=4.0,
            execution=1,
            value_prop=3,
        )
    # 4 reviews del local "Tecnico": execution=3, value=1
    for _ in range(4):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid_high_exec,
            restaurant_name="Resto Tecnico Sort",
            dish_name="Plato Caro",
            score=4.5,
            execution=3,
            value_prop=1,
        )

    r = await async_client_integration.get(
        f"/api/restaurants/in-bbox?{BBOX_PARAMS}&sort=value_prop"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    names = [it["name"] for it in items if it["name"] in ("Resto Ganga", "Resto Tecnico Sort")]
    assert names[0] == "Resto Ganga", f"order was {names}"


@pytest.mark.asyncio
async def test_bbox_sort_trending_orders_by_recent_reviews(async_client_integration):
    """sort=trending rankea primero los locales con más reviews en las últimas 48h.

    Como los reviews que crea el test son nuevos por definición, basta con
    que el local con MÁS reviews aparezca primero (todos sus reviews caen
    dentro de la ventana 48h)."""
    pid_buzz = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_quiet = f"pytest_place_{uuid.uuid4().hex[:8]}"

    # 5 reviews al local "Buzz"
    for _ in range(5):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid_buzz,
            restaurant_name="Resto Buzz",
            dish_name="Plato Buzz",
            score=4.0,
            execution=2,
            value_prop=2,
        )
    # 1 review al local "Quiet"
    u = await register_and_login(async_client_integration)
    await _post_review_with_pillars(
        async_client_integration,
        u.cookies,
        place_id=pid_quiet,
        restaurant_name="Resto Quiet",
        dish_name="Plato Quiet",
        score=4.0,
        execution=2,
        value_prop=2,
    )

    r = await async_client_integration.get(
        f"/api/restaurants/in-bbox?{BBOX_PARAMS}&sort=trending"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    names = [it["name"] for it in items if it["name"] in ("Resto Buzz", "Resto Quiet")]
    assert names[0] == "Resto Buzz", f"order was {names}"

    buzz = next(it for it in items if it["name"] == "Resto Buzz")
    assert buzz["trending_count"] >= 5


@pytest.mark.asyncio
async def test_bbox_invalid_sort_returns_400(async_client_integration):
    r = await async_client_integration.get(
        f"/api/restaurants/in-bbox?{BBOX_PARAMS}&sort=invalid_key"
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_bbox_include_empty_adds_restaurants_without_reviews(async_client_integration):
    """include_empty=true incluye locales sin reviews con is_empty=true.

    Si el endpoint de creación de restaurantes requiere admin, skipeamos
    (no podemos preparar el fixture)."""
    user = await register_and_login(async_client_integration)
    pid_with = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_empty = f"pytest_place_{uuid.uuid4().hex[:8]}"

    await _post_review_with_pillars(
        async_client_integration,
        user.cookies,
        place_id=pid_with,
        restaurant_name="Resto Con Reviews",
        dish_name="Plato",
        score=4.0,
        execution=2,
    )

    create_resp = await async_client_integration.post(
        "/api/restaurants",
        json={
            "google_place_id": pid_empty,
            "name": "Resto Vacio",
            "city": "Buenos Aires",
            "latitude": -34.6,
            "longitude": -58.4,
        },
        cookies=user.cookies,
    )
    if create_resp.status_code in (401, 403):
        pytest.skip("Restaurant create requires admin/critic role")
    assert create_resp.status_code in (200, 201), create_resp.text

    # Sin include_empty: solo el con reviews aparece.
    r1 = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r1.status_code == 200
    names1 = {it["name"] for it in r1.json()["items"]}
    assert "Resto Con Reviews" in names1
    assert "Resto Vacio" not in names1

    # Con include_empty: ambos aparecen, el vacío con is_empty=True.
    r2 = await async_client_integration.get(
        f"/api/restaurants/in-bbox?{BBOX_PARAMS}&include_empty=true"
    )
    assert r2.status_code == 200
    by_name = {it["name"]: it for it in r2.json()["items"]}
    assert "Resto Con Reviews" in by_name
    assert "Resto Vacio" in by_name
    assert by_name["Resto Vacio"]["is_empty"] is True
    assert by_name["Resto Vacio"]["golden_dish"] is None
    assert by_name["Resto Con Reviews"]["is_empty"] is False


# --- Fase 4: chef_only=true filtra a restos con Chef Badge --------------------


@pytest.mark.asyncio
async def test_bbox_chef_only_keeps_only_chef_restos(async_client_integration):
    """chef_only=true filtra a locales con al menos un plato Chef Badge.

    Setup: 2 restos en CABA — uno con 5 reviews execution=3 (gana Chef
    Badge), otro con 1 review execution=3 (no califica por shrinkage).
    """
    pid_chef = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_no_chef = f"pytest_place_{uuid.uuid4().hex[:8]}"

    # Resto con Chef Badge: 5 reviews exec=3 sobre el mismo plato.
    for _ in range(5):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid_chef,
            restaurant_name="Resto Chef Filter",
            dish_name="Plato Chef",
            score=4.5,
            execution=3,
        )
    # Resto sin Chef Badge: 1 sola review exec=3 (no llega al MIN_REVIEWS_FOR_BADGE).
    u = await register_and_login(async_client_integration)
    await _post_review_with_pillars(
        async_client_integration,
        u.cookies,
        place_id=pid_no_chef,
        restaurant_name="Resto Sin Chef Filter",
        dish_name="Plato",
        score=5.0,
        execution=3,
    )

    # Sin chef_only ambos aparecen.
    r_all = await async_client_integration.get(f"/api/restaurants/in-bbox?{BBOX_PARAMS}")
    assert r_all.status_code == 200
    names_all = {it["name"] for it in r_all.json()["items"]}
    assert "Resto Chef Filter" in names_all
    assert "Resto Sin Chef Filter" in names_all

    # Con chef_only=true solo aparece el Chef.
    r_chef = await async_client_integration.get(
        f"/api/restaurants/in-bbox?{BBOX_PARAMS}&chef_only=true"
    )
    assert r_chef.status_code == 200
    items = r_chef.json()["items"]
    names_chef = {it["name"] for it in items}
    assert "Resto Chef Filter" in names_chef
    assert "Resto Sin Chef Filter" not in names_chef
    # Cualquier resto en la respuesta debe tener has_chef_badge=True.
    for it in items:
        assert it["has_chef_badge"] is True, (
            f"{it['name']} apareció con chef_only=true pero no tiene Chef Badge"
        )


@pytest.mark.asyncio
async def test_bbox_chef_only_overrides_include_empty(async_client_integration):
    """Locales sin reviews no pueden tener Chef Badge; chef_only los excluye
    incluso con include_empty=true."""
    user = await register_and_login(async_client_integration)
    pid_chef = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_empty = f"pytest_place_{uuid.uuid4().hex[:8]}"

    # Resto con Chef Badge.
    for _ in range(4):
        u = await register_and_login(async_client_integration)
        await _post_review_with_pillars(
            async_client_integration,
            u.cookies,
            place_id=pid_chef,
            restaurant_name="Resto Chef Override",
            dish_name="Plato Chef",
            score=5.0,
            execution=3,
        )

    # Resto vacío (sin reviews) — requiere endpoint de creación de restaurant.
    create_resp = await async_client_integration.post(
        "/api/restaurants",
        json={
            "google_place_id": pid_empty,
            "name": "Resto Vacio Chef Test",
            "city": "Buenos Aires",
            "latitude": -34.6,
            "longitude": -58.4,
        },
        cookies=user.cookies,
    )
    if create_resp.status_code in (401, 403):
        pytest.skip("Restaurant create requires admin/critic role")
    assert create_resp.status_code in (200, 201), create_resp.text

    # chef_only=true + include_empty=true → el empty sigue suprimido.
    r = await async_client_integration.get(
        f"/api/restaurants/in-bbox?{BBOX_PARAMS}&chef_only=true&include_empty=true"
    )
    assert r.status_code == 200
    names = {it["name"] for it in r.json()["items"]}
    assert "Resto Chef Override" in names
    assert "Resto Vacio Chef Test" not in names
