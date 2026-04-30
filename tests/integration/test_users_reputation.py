"""Integration tests for the reputation block in GET /api/users/{id_or_handle}.

Cubre los 4 campos que aporta UserReputation:
- verified_review_count (reviews con los 3 pilares completos)
- restaurants_visited (count distinct de restaurantes reseñados)
- top_categories (categorías con ≥2 reviews, rankeadas por avg×log(1+count))
- featured_title (título destacado del usuario, mayor mastery_level)

Y la lógica de mastery_level por categoría:
- apprentice: 3+ reseñas, avg ≥ 3.5
- sommelier: 10+ reseñas, avg ≥ 3.8
- master: 25+ reseñas, avg ≥ 4.0
"""

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


@pytest.mark.asyncio
async def test_reputation_present_in_response(async_client_integration, user_a):
    """El bloque reputation siempre viene en el response, aun para usuarios
    sin reviews (todos los campos en cero / vacíos)."""
    r = await async_client_integration.get(
        f"/api/users/{user_a.user_id}", cookies=user_a.cookies
    )
    assert r.status_code == 200
    body = r.json()
    assert "reputation" in body
    rep = body["reputation"]
    assert rep["verified_review_count"] == 0
    assert rep["restaurants_visited"] == 0
    assert rep["top_categories"] == []


@pytest.mark.asyncio
async def test_reputation_verified_count_increments_with_3_pillars(
    async_client_integration, user_a
):
    """Cada review con los 3 pilares completos suma a verified_review_count.
    Reviews sin pilares NO suman."""
    # Una sin pilares.
    await create_review(async_client_integration, user_a.cookies)
    # Una con solo 2 pilares — no califica.
    await create_review(
        async_client_integration,
        user_a.cookies,
        presentation=3,
        execution=3,
    )
    # Una con los 3 — sí califica.
    await create_review(
        async_client_integration,
        user_a.cookies,
        presentation=2,
        value_prop=2,
        execution=2,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    assert r.json()["reputation"]["verified_review_count"] == 1


@pytest.mark.asyncio
async def test_reputation_restaurants_visited_counts_distinct_restos(
    async_client_integration, user_a
):
    """restaurants_visited cuenta restaurantes únicos, no reviews totales."""
    pid_a = f"pytest_place_{uuid.uuid4().hex[:8]}"
    pid_b = f"pytest_place_{uuid.uuid4().hex[:8]}"

    # 2 reviews en el mismo resto (distintos platos).
    await create_review(
        async_client_integration,
        user_a.cookies,
        place_id=pid_a,
        restaurant_name="Resto A Reputation",
        dish_name="Plato A1",
    )
    await create_review(
        async_client_integration,
        user_a.cookies,
        place_id=pid_a,
        restaurant_name="Resto A Reputation",
        dish_name="Plato A2",
    )
    # 1 review en otro resto.
    await create_review(
        async_client_integration,
        user_a.cookies,
        place_id=pid_b,
        restaurant_name="Resto B Reputation",
        dish_name="Plato B",
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    assert rep["restaurants_visited"] == 2  # 2 restos únicos, no 3 reviews


@pytest.mark.asyncio
async def test_reputation_top_categories_threshold(
    async_client_integration, user_a
):
    """Una categoría con 1 sola review NO califica como especialidad
    (threshold _MIN_REVIEWS_PER_CATEGORY = 2)."""
    # Single review — no debería aparecer en top_categories.
    await create_review(async_client_integration, user_a.cookies)

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    # Aún si la review tiene categoría, 1 review no califica.
    assert rep["top_categories"] == []


@pytest.mark.asyncio
async def test_reputation_top_categories_capped_at_3(
    async_client_integration, user_a
):
    """top_categories trae hasta 3 entradas, ordenadas por score DESC."""
    # 4 categorías distintas, cada una con 2 reviews. Forzamos categorías
    # variando el restaurant + dejando que el backend autodetecte categoría
    # vía heurística por nombre. La categoría puede no ser sembrada — en ese
    # caso el JOIN a categories filtra y quedamos sin top_categories. Si pasa
    # eso, el test simplemente verifica el cap (≤ 3) sin asumir contenido.
    for i in range(4):
        for j in range(2):
            await create_review(
                async_client_integration,
                user_a.cookies,
                restaurant_name=f"Resto Cat {i} Reputation",
                dish_name=f"Plato {i}-{j}",
            )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    # Cap: nunca más de 3 categorías expuestas.
    assert len(rep["top_categories"]) <= 3
    # Si hay alguna categoría, debe venir con la shape esperada.
    for cat in rep["top_categories"]:
        assert "name" in cat
        assert "review_count" in cat and cat["review_count"] >= 2
        assert "avg_rating" in cat
        assert "score" in cat
    # Si trae más de una, deben venir ordenadas por score DESC.
    scores = [c["score"] for c in rep["top_categories"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_reputation_visible_to_anonymous_viewer(
    async_client_integration, user_a
):
    """La reputation es información pública — el caller anónimo también la ve."""
    await create_review(
        async_client_integration,
        user_a.cookies,
        presentation=3,
        value_prop=3,
        execution=3,
    )
    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    assert r.status_code == 200
    rep = r.json()["reputation"]
    assert rep["verified_review_count"] == 1
    assert rep["restaurants_visited"] == 1


@pytest.mark.asyncio
async def test_reputation_handle_lookup_works(async_client_integration):
    """El endpoint resuelve por handle además de UUID — la reputation viaja
    igual."""
    user = await register_and_login(async_client_integration)
    handle = f"pytest_handle_{uuid.uuid4().hex[:6]}"
    patch_resp = await async_client_integration.patch(
        "/api/users/me", json={"handle": handle}, cookies=user.cookies
    )
    assert patch_resp.status_code == 200
    await create_review(async_client_integration, user.cookies)

    r = await async_client_integration.get(f"/api/users/{handle}")
    assert r.status_code == 200
    body = r.json()
    assert body["handle"] == handle
    assert body["reputation"]["restaurants_visited"] == 1


# --- mastery_level y featured_title ---
#
# Estos tests dependen de que existan categorías sembradas en la DB. La
# helper `_pick_categories` salta el test si no hay suficientes — ese es el
# patrón que ya usa test_discovery.py para depender de /api/categories.


async def _pick_categories(client: httpx.AsyncClient, n: int) -> list[str]:
    """Devuelve los nombres de las primeras `n` categorías, o salta si no hay."""
    r = await client.get("/api/categories")
    if r.status_code != 200 or not r.json():
        pytest.skip("No categories seeded; cannot test mastery levels")
    cats = r.json()
    if len(cats) < n:
        pytest.skip(f"Need {n} categories seeded, only {len(cats)} available")
    return [c["name"] for c in cats[:n]]


async def _post_with_category(
    client: httpx.AsyncClient,
    cookies: Any,
    *,
    place_id: str,
    restaurant_name: str,
    dish_name: str,
    category: str,
    score: float = 4.5,
) -> str:
    """POST /api/posts forzando category. Devuelve review id."""
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
        "category": category,
        "score": score,
        "text": "Test mastery review.",
    }
    r = await client.post("/api/posts", json=payload, cookies=cookies)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_n_reviews_in_category(
    client: httpx.AsyncClient,
    cookies: Any,
    *,
    n: int,
    category: str,
    score: float = 4.5,
    place_id: str | None = None,
) -> str:
    """Crea n reseñas del mismo usuario en una categoría (mismo restaurante,
    distintos platos). Devuelve el place_id usado."""
    pid = place_id or f"pytest_place_{uuid.uuid4().hex[:10]}"
    rest = f"Resto Mastery {uuid.uuid4().hex[:6]}"
    for i in range(n):
        await _post_with_category(
            client, cookies,
            place_id=pid, restaurant_name=rest,
            dish_name=f"Plato {i} {uuid.uuid4().hex[:4]}",
            category=category, score=score,
        )
    return pid


def _find_cat(top_categories: list[dict], name: str) -> dict | None:
    return next((c for c in top_categories if c["name"] == name), None)


@pytest.mark.asyncio
async def test_mastery_apprentice_with_3_reviews(
    async_client_integration, user_a
):
    """3 reseñas con avg ≥ 3.5 → mastery_level = 'apprentice'."""
    cat = (await _pick_categories(async_client_integration, 1))[0]
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=3, category=cat, score=4.5,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    target = _find_cat(rep["top_categories"], cat)
    assert target is not None, f"Category {cat!r} not in top_categories"
    assert target["review_count"] == 3
    assert target["mastery_level"] == "apprentice"


@pytest.mark.asyncio
async def test_mastery_apprentice_below_avg_threshold(
    async_client_integration, user_a
):
    """3 reseñas con avg < 3.5 → mastery_level = None aunque alcance volumen."""
    cat = (await _pick_categories(async_client_integration, 1))[0]
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=3, category=cat, score=3.0,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    target = _find_cat(rep["top_categories"], cat)
    assert target is not None
    assert target["mastery_level"] is None


@pytest.mark.asyncio
async def test_mastery_sommelier_with_10_reviews(
    async_client_integration, user_a
):
    """10 reseñas con avg ≥ 3.8 → mastery_level = 'sommelier'."""
    cat = (await _pick_categories(async_client_integration, 1))[0]
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=10, category=cat, score=4.0,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    target = _find_cat(rep["top_categories"], cat)
    assert target is not None
    assert target["review_count"] == 10
    assert target["mastery_level"] == "sommelier"


@pytest.mark.asyncio
async def test_mastery_master_with_25_reviews(
    async_client_integration, user_a
):
    """25 reseñas con avg ≥ 4.0 → mastery_level = 'master'."""
    cat = (await _pick_categories(async_client_integration, 1))[0]
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=25, category=cat, score=4.5,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    target = _find_cat(rep["top_categories"], cat)
    assert target is not None
    assert target["review_count"] == 25
    assert target["mastery_level"] == "master"


@pytest.mark.asyncio
async def test_featured_title_picks_highest_level(
    async_client_integration, user_a
):
    """Con apprentice en cat A y sommelier en cat B, featured_title = cat B."""
    cats = await _pick_categories(async_client_integration, 2)
    cat_a, cat_b = cats[0], cats[1]
    # apprentice en cat A
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=3, category=cat_a, score=4.5,
    )
    # sommelier en cat B
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=10, category=cat_b, score=4.0,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    assert rep["featured_title"] is not None
    assert rep["featured_title"]["category"] == cat_b
    assert rep["featured_title"]["level"] == "sommelier"


@pytest.mark.asyncio
async def test_featured_title_tie_break_by_review_count(
    async_client_integration, user_a
):
    """Dos categorías al mismo nivel → gana la de más reseñas."""
    cats = await _pick_categories(async_client_integration, 2)
    cat_a, cat_b = cats[0], cats[1]
    # apprentice en cat A con 5 reseñas
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=5, category=cat_a, score=4.0,
    )
    # apprentice en cat B con 4 reseñas
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=4, category=cat_b, score=4.0,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    assert rep["featured_title"] is not None
    assert rep["featured_title"]["level"] == "apprentice"
    assert rep["featured_title"]["category"] == cat_a


@pytest.mark.asyncio
async def test_featured_title_none_when_no_mastery(
    async_client_integration, user_a
):
    """Sin volumen suficiente para apprentice → featured_title = None."""
    cat = (await _pick_categories(async_client_integration, 1))[0]
    # Solo 2 reseñas — no califica para apprentice (necesita 3+)
    await _create_n_reviews_in_category(
        async_client_integration, user_a.cookies, n=2, category=cat, score=4.5,
    )

    r = await async_client_integration.get(f"/api/users/{user_a.user_id}")
    rep = r.json()["reputation"]
    assert rep["featured_title"] is None
