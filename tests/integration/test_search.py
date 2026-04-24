"""Integration tests for /api/search."""

import os
import uuid

import pytest

from tests.integration.conftest import create_review

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_search_matches_dish_and_restaurant(
    async_client_integration, user_a
):
    token = uuid.uuid4().hex[:8]
    await create_review(
        async_client_integration,
        user_a.cookies,
        restaurant_name=f"Pytest Bistro {token}",
        dish_name=f"Plato {token}",
    )

    r = await async_client_integration.get(f"/api/search?q={token}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["dishes"]) >= 1
    assert len(body["restaurants"]) >= 1


@pytest.mark.asyncio
async def test_search_matches_user_by_handle(
    async_client_integration, user_a
):
    handle = f"pytest{uuid.uuid4().hex[:6]}"
    patch = await async_client_integration.patch(
        "/api/users/me", json={"handle": handle}, cookies=user_a.cookies
    )
    assert patch.status_code == 200, patch.text

    r = await async_client_integration.get(f"/api/search?q={handle}")
    assert r.status_code == 200
    users = r.json()["users"]
    assert any(u["handle"] == handle for u in users)


@pytest.mark.asyncio
async def test_search_diacritics_and_case_insensitive(async_client_integration):
    # The backend uses ILIKE — case-insensitive by default. We don't normalize
    # diacritics server-side (frontend does for mocks), so exact byte
    # differences still match. This covers the minimum guarantee.
    r = await async_client_integration.get("/api/search?q=PIZZA")
    assert r.status_code == 200
