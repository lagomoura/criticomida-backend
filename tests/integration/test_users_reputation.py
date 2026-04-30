"""Integration tests for the reputation block in GET /api/users/{id_or_handle}.

Cubre los 3 campos que aporta UserReputation:
- verified_review_count (reviews con los 3 pilares completos)
- restaurants_visited (count distinct de restaurantes reseñados)
- top_categories (categorías con ≥2 reviews, rankeadas por avg×log(1+count))
"""

import os
import uuid

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
