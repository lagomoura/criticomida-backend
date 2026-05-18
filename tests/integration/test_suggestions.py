"""Integration tests for GET /api/users/me/suggestions (people-you-may-know).

Cubre las dos señales (friends-of-friends, co-reviewers) y las exclusiones
de safety (block / mute) más el "ya seguido" y "self".
"""

import os

import pytest

from tests.integration.conftest import (
    create_review,
    register_and_login,
)

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_suggestions_cold_start_uses_fallback(
    async_client_integration, user_a
):
    """Usuario nuevo sin grafo ni reseñas.

    Antes devolvía ``[]`` (rail oculto). Ahora el cold-start se rellena
    con un fallback de popularidad/críticos, así que un viewer recién
    registrado igual recibe candidatos — y todos ellos sin señal
    compartida y marcados con ``reason_kind`` de fallback.
    """
    # Garantizamos que exista al menos otro usuario elegible.
    await register_and_login(async_client_integration)

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    for it in items:
        assert it["id"] != user_a.user_id
        # Sin grafo ni reseñas no puede haber señal compartida.
        assert it["shared_followers"] == 0
        assert it["shared_restaurants"] == 0
        assert it["reason_kind"] in ("popular_critic", "popular")


@pytest.mark.asyncio
async def test_suggestions_surface_friends_of_friends(
    async_client_integration, user_a, user_b
):
    """A sigue a B; B sigue a C. /me/suggestions de A debe incluir a C."""
    user_c = await register_and_login(async_client_integration)
    # B sigue a C
    await async_client_integration.post(
        f"/api/users/{user_c.user_id}/follow", cookies=user_b.cookies
    )
    # A sigue a B
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    assert r.status_code == 200
    items = r.json()["items"]
    candidate = next((it for it in items if it["id"] == user_c.user_id), None)
    assert candidate is not None
    assert candidate["shared_followers"] >= 1
    assert candidate["reason_kind"] == "signal"


@pytest.mark.asyncio
async def test_suggestions_surface_co_reviewers(
    async_client_integration, user_a, user_b
):
    """A y B reseñan platos del mismo restaurante. /me/suggestions de A
    debe incluir a B con shared_restaurants >= 1."""
    place_id = "pytest_place_coreview_abc"
    await create_review(async_client_integration, user_a.cookies, place_id=place_id)
    await create_review(async_client_integration, user_b.cookies, place_id=place_id)

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    assert r.status_code == 200
    items = r.json()["items"]
    candidate = next((it for it in items if it["id"] == user_b.user_id), None)
    assert candidate is not None
    assert candidate["shared_restaurants"] >= 1
    assert candidate["reason_kind"] == "signal"


@pytest.mark.asyncio
async def test_suggestions_exclude_already_followed(
    async_client_integration, user_a, user_b
):
    """B aparecería por FoF pero A ya lo sigue → no debe aparecer."""
    user_c = await register_and_login(async_client_integration)
    await async_client_integration.post(
        f"/api/users/{user_c.user_id}/follow", cookies=user_b.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    items = r.json()["items"]
    assert all(it["id"] != user_b.user_id for it in items)


@pytest.mark.asyncio
async def test_suggestions_exclude_blocked(
    async_client_integration, user_a, user_b
):
    """Co-reviewer que A bloqueó NO debe aparecer en suggestions."""
    place_id = "pytest_place_blocked_xyz"
    await create_review(async_client_integration, user_a.cookies, place_id=place_id)
    await create_review(async_client_integration, user_b.cookies, place_id=place_id)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    items = r.json()["items"]
    assert all(it["id"] != user_b.user_id for it in items)


@pytest.mark.asyncio
async def test_suggestions_exclude_muted(
    async_client_integration, user_a, user_b
):
    """Co-reviewer que A muteó tampoco aparece."""
    place_id = "pytest_place_muted_lmn"
    await create_review(async_client_integration, user_a.cookies, place_id=place_id)
    await create_review(async_client_integration, user_b.cookies, place_id=place_id)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    items = r.json()["items"]
    assert all(it["id"] != user_b.user_id for it in items)


@pytest.mark.asyncio
async def test_suggestions_exclude_self(async_client_integration, user_a, user_b):
    """A nunca debería aparecer en sus propias sugerencias."""
    place_id = "pytest_place_self_qrs"
    await create_review(async_client_integration, user_a.cookies, place_id=place_id)
    await create_review(async_client_integration, user_b.cookies, place_id=place_id)

    r = await async_client_integration.get(
        "/api/users/me/suggestions", cookies=user_a.cookies
    )
    items = r.json()["items"]
    assert all(it["id"] != user_a.user_id for it in items)


@pytest.mark.asyncio
async def test_suggestions_requires_auth(async_client_integration):
    async_client_integration.cookies.clear()
    r = await async_client_integration.get("/api/users/me/suggestions")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_suggestions_limit_caps_response(
    async_client_integration, user_a
):
    r = await async_client_integration.get(
        "/api/users/me/suggestions?limit=5", cookies=user_a.cookies
    )
    assert r.status_code == 200
    assert len(r.json()["items"]) <= 5
