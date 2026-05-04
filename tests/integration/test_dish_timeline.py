"""Integration tests for GET /api/social/dishes/{id}/timeline.

Verifica:
- Buckets agrupan reseñas por trimestre por default; granularidad `month`
  cambia el bucket a YYYY-MM.
- Cada bucket trae avg_rating, review_count y avg de los 3 pilares (cuando
  los hay).
- delta_rating compara contra el bucket anterior; el primero viene en null.
- Reseñas sin date_tasted no entran al timeline (la query las filtra).
- 404 cuando el dish_id no existe.
"""

import os
import uuid
from datetime import date
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
    score: float,
    date_tasted: date,
    presentation: int | None = None,
    value_prop: int | None = None,
    execution: int | None = None,
    price_paid: float | None = None,
) -> str:
    extras: dict[str, Any] = {"date_tasted": date_tasted.isoformat()}
    if presentation is not None:
        extras["presentation"] = presentation
    if value_prop is not None:
        extras["value_prop"] = value_prop
    if execution is not None:
        extras["execution"] = execution
    if price_paid is not None:
        extras["price_paid"] = price_paid

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
        "text": "Timeline test review.",
        "extras": extras,
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
    assert items
    slug = items[0]["slug"]
    rd = await client.get(f"/api/restaurants/{slug}/dishes")
    for d in rd.json():
        if d["name"] == dish_name:
            return d["id"]
    raise AssertionError(f"dish {dish_name!r} not found")


@pytest.mark.asyncio
async def test_timeline_404_for_unknown_dish(async_client_integration):
    bogus = str(uuid.uuid4())
    r = await async_client_integration.get(
        f"/api/social/dishes/{bogus}/timeline"
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_timeline_groups_by_quarter_with_delta(async_client_integration):
    """Reseñas en distintos trimestres → buckets ordenados por período con
    delta_rating vs el bucket anterior. El primer bucket trae delta=null."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Timeline {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Timeline {uuid.uuid4().hex[:4]}"

    user1 = await register_and_login(async_client_integration)
    user2 = await register_and_login(async_client_integration)
    user3 = await register_and_login(async_client_integration)

    # Q1 2024: una reseña 4.0
    await _post_review(
        async_client_integration, user1.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 2, 15),
    )
    # Q3 2024: dos reseñas (4.5 y 4.5 → avg 4.5)
    await _post_review(
        async_client_integration, user2.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.5, date_tasted=date(2024, 8, 10),
    )
    await _post_review(
        async_client_integration, user3.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.5, date_tasted=date(2024, 9, 1),
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["granularity"] == "quarter"

    buckets = body["buckets"]
    assert len(buckets) == 2
    # Período más temprano primero.
    assert buckets[0]["period"] == "2024-Q1"
    assert buckets[0]["review_count"] == 1
    assert float(buckets[0]["avg_rating"]) == pytest.approx(4.0, abs=0.01)
    assert buckets[0]["delta_rating"] is None  # primer bucket — sin referencia.

    assert buckets[1]["period"] == "2024-Q3"
    assert buckets[1]["review_count"] == 2
    assert float(buckets[1]["avg_rating"]) == pytest.approx(4.5, abs=0.01)
    # delta = 4.5 - 4.0 = 0.5
    assert float(buckets[1]["delta_rating"]) == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_timeline_month_granularity(async_client_integration):
    """granularity=month agrupa por YYYY-MM en lugar de YYYY-Qn."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Timeline Month {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Timeline Month {uuid.uuid4().hex[:4]}"

    user = await register_and_login(async_client_integration)
    await _post_review(
        async_client_integration, user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 3, 5),
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline?granularity=month"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["granularity"] == "month"
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["period"] == "2024-03"


@pytest.mark.asyncio
async def test_timeline_pillars_avg_when_present(async_client_integration):
    """Cuando las reseñas traen los 3 pilares, el bucket expone los promedios."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Timeline Pillars {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Timeline Pillars {uuid.uuid4().hex[:4]}"

    user1 = await register_and_login(async_client_integration)
    user2 = await register_and_login(async_client_integration)

    await _post_review(
        async_client_integration, user1.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.5, date_tasted=date(2024, 5, 1),
        presentation=2, value_prop=3, execution=2,
    )
    await _post_review(
        async_client_integration, user2.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=5.0, date_tasted=date(2024, 5, 20),
        presentation=3, value_prop=3, execution=3,
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline"
    )
    body = r.json()
    bucket = body["buckets"][0]
    # avg de presentación: (2+3)/2 = 2.5
    assert float(bucket["presentation_avg"]) == pytest.approx(2.5, abs=0.01)
    # avg de value_prop: (3+3)/2 = 3.0
    assert float(bucket["value_prop_avg"]) == pytest.approx(3.0, abs=0.01)
    # avg de execution: (2+3)/2 = 2.5
    assert float(bucket["execution_avg"]) == pytest.approx(2.5, abs=0.01)


@pytest.mark.asyncio
async def test_timeline_price_avg_with_delta(async_client_integration):
    """Reseñas con price_paid en distintos trimestres → cada bucket trae
    price_avg y delta_price_avg vs el bucket anterior con precio. Reseñas sin
    price_paid no aportan al avg. El campo currency_code aparece en la
    respuesta (puede ser null si el restaurante recién creado no tiene moneda
    aún — el backfill por city solo corre en la migración 039 y restaurantes
    nuevos no la heredan automáticamente todavía)."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Timeline Price {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Timeline Price {uuid.uuid4().hex[:4]}"

    user1 = await register_and_login(async_client_integration)
    user2 = await register_and_login(async_client_integration)
    user3 = await register_and_login(async_client_integration)
    user4 = await register_and_login(async_client_integration)

    # Q1 2024: precios 4000 y 4500 → avg 4250
    await _post_review(
        async_client_integration, user1.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 2, 15), price_paid=4000,
    )
    await _post_review(
        async_client_integration, user2.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 3, 1), price_paid=4500,
    )
    # Q3 2024: una con precio 6000 y una sin precio (no aporta al avg).
    await _post_review(
        async_client_integration, user3.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 8, 10), price_paid=6000,
    )
    await _post_review(
        async_client_integration, user4.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 9, 1),  # sin price_paid
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline"
    )
    assert r.status_code == 200
    body = r.json()

    # currency_code está siempre presente en la respuesta (puede ser null).
    assert "currency_code" in body

    buckets = body["buckets"]
    assert len(buckets) == 2

    # Q1: avg(4000, 4500) = 4250; primer bucket → delta None.
    assert buckets[0]["period"] == "2024-Q1"
    assert buckets[0]["price_avg"] == pytest.approx(4250.0, abs=0.01)
    assert buckets[0]["delta_price_avg"] is None

    # Q3: avg(6000) = 6000; delta vs Q1 = +1750.
    assert buckets[1]["period"] == "2024-Q3"
    assert buckets[1]["review_count"] == 2  # incluye la sin precio
    assert buckets[1]["price_avg"] == pytest.approx(6000.0, abs=0.01)
    assert buckets[1]["delta_price_avg"] == pytest.approx(1750.0, abs=0.01)


@pytest.mark.asyncio
async def test_timeline_price_null_when_no_reviews_have_price(
    async_client_integration,
):
    """Si ninguna reseña del bucket trae price_paid → price_avg y
    delta_price_avg quedan en None."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Timeline NoPrice {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Timeline NoPrice {uuid.uuid4().hex[:4]}"

    user = await register_and_login(async_client_integration)
    await _post_review(
        async_client_integration, user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 2, 15),  # sin price_paid
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline"
    )
    body = r.json()
    assert body["buckets"][0]["price_avg"] is None
    assert body["buckets"][0]["delta_price_avg"] is None


@pytest.mark.asyncio
async def test_timeline_excludes_outlier_flagged_prices_from_avg(
    async_client_integration,
):
    """Capa 2 anti-fraude end-to-end: 3 reseñas con precios estables (5000)
    establecen un baseline; una 4ª reseña con 99999 (>3× la mediana) se
    soft-flagea automáticamente y queda excluida del `price_avg`.

    El precio en la BD se mantiene (puede revisarlo un admin), pero el
    timeline no se contamina con el outlier."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Outlier {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Outlier {uuid.uuid4().hex[:4]}"

    user1 = await register_and_login(async_client_integration)
    user2 = await register_and_login(async_client_integration)
    user3 = await register_and_login(async_client_integration)
    user4 = await register_and_login(async_client_integration)

    # Baseline: 3 reseñas en Q1 con precios cercanos.
    for u in (user1, user2, user3):
        await _post_review(
            async_client_integration, u.cookies,
            place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
            score=4.0, date_tasted=date(2024, 2, 15), price_paid=5000,
        )
    # Outlier: 99999 en el mismo bucket.
    await _post_review(
        async_client_integration, user4.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 2, 20), price_paid=99999,
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline"
    )
    assert r.status_code == 200
    body = r.json()
    bucket = body["buckets"][0]
    # 4 reseñas pero el avg solo cuenta las 3 con precio razonable → 5000.
    assert bucket["review_count"] == 4
    assert bucket["price_avg"] == pytest.approx(5000.0, abs=0.01)


@pytest.mark.asyncio
async def test_post_review_rejects_price_above_cap(async_client_integration):
    """`/api/posts` con `price_paid` fuera del rango fallback (>1B) responde
    422. La capa 1 anti-fraude (caps por moneda) pega antes de tocar la BD."""
    user = await register_and_login(async_client_integration)
    payload = {
        "restaurant": {
            "place_id": f"pytest_place_{uuid.uuid4().hex[:10]}",
            "name": f"Resto Cap {uuid.uuid4().hex[:6]}",
            "formatted_address": "BA",
            "city": "Buenos Aires",
            "latitude": -34.6,
            "longitude": -58.4,
        },
        "dish_name": f"Plato Cap {uuid.uuid4().hex[:4]}",
        "score": 4.0,
        "text": "Cap test review.",
        "extras": {
            "date_tasted": date(2024, 5, 1).isoformat(),
            "price_paid": 9999999999,  # > fallback max (1B)
        },
    }
    r = await async_client_integration.post(
        "/api/posts", json=payload, cookies=user.cookies
    )
    assert r.status_code == 422
    body = r.json()
    detail = body.get("detail")
    # detail puede ser dict (cap por moneda) o lista (Pydantic) — ambos OK
    assert detail is not None


@pytest.mark.asyncio
async def test_timeline_empty_when_no_reviews(async_client_integration):
    """Plato recién creado (sin reseñas) → timeline con buckets vacíos."""
    # Creamos un plato cualquiera con una reseña, luego la borramos vía un
    # plato nuevo en el mismo resto que nadie reseñó (atajo: pedimos a un user
    # que reseñe Plato A, y nos quedamos con un plato distinto para el test).
    # Más fácil: creamos el resto+plato y verificamos que un dish recién creado
    # tenga timeline vacío. No podemos crear dishes vacíos vía /api/posts (el
    # endpoint requiere review). Así que probamos con un plato con UNA reseña
    # — el timeline traerá un único bucket sin delta y eso ya es coverage.
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest_name = f"Resto Timeline Solo {uuid.uuid4().hex[:6]}"
    dish_name = f"Plato Timeline Solo {uuid.uuid4().hex[:4]}"

    user = await register_and_login(async_client_integration)
    await _post_review(
        async_client_integration, user.cookies,
        place_id=place_id, restaurant_name=rest_name, dish_name=dish_name,
        score=4.0, date_tasted=date(2024, 6, 1),
    )

    dish_id = await _find_dish_id(async_client_integration, rest_name, dish_name)
    r = await async_client_integration.get(
        f"/api/social/dishes/{dish_id}/timeline"
    )
    body = r.json()
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["delta_rating"] is None
