"""Integration tests for /api/trending."""

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
async def test_cities_list_includes_active_city(
    async_client_integration, user_a
):
    city = f"Pytest City {uuid.uuid4().hex[:6]}"
    await create_review(async_client_integration, user_a.cookies, city=city)
    r = await async_client_integration.get("/api/trending/cities")
    assert r.status_code == 200
    assert any(c["city"] == city for c in r.json()["items"])


@pytest.mark.asyncio
async def test_dishes_ranked_by_priority(
    async_client_integration, user_a, user_b
):
    city = f"Pytest City {uuid.uuid4().hex[:6]}"
    review_id = await create_review(
        async_client_integration, user_a.cookies, city=city
    )
    # user_b likes it → priority > 0
    await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    r = await async_client_integration.get(
        f"/api/trending/dishes?city={city}&days=7"
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    first = items[0]
    assert first["priority"] > 0
    # Expected: 1 like (×1) + 1 review (×3) = 4
    assert first["priority"] == 4


@pytest.mark.asyncio
async def test_unknown_city_empty(async_client_integration):
    r = await async_client_integration.get(
        f"/api/trending/dishes?city={uuid.uuid4().hex}&days=7"
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_city_param_required(async_client_integration):
    r = await async_client_integration.get("/api/trending/dishes")
    assert r.status_code == 422
