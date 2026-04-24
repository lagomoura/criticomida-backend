"""Integration tests for the follows router."""

import os

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 and a reachable DATABASE_URL "
        "to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_follow_increments_counter(async_client_integration, user_a, user_b):
    r = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["following"] is True
    assert body["followers_count"] >= 1


@pytest.mark.asyncio
async def test_follow_is_idempotent(async_client_integration, user_a, user_b):
    first = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    second = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    assert second.status_code == 200
    # Counter should not double on a repeat follow.
    assert second.json()["followers_count"] == first.json()["followers_count"]


@pytest.mark.asyncio
async def test_self_follow_rejected(async_client_integration, user_a):
    r = await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_a.cookies
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unfollow_is_idempotent(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    first = await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    second = await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["following"] is False


@pytest.mark.asyncio
async def test_follow_requires_auth(async_client_integration, user_b):
    # The shared client persists cookies from earlier logins; clear them to
    # simulate an unauthenticated request.
    async_client_integration.cookies.clear()
    r = await async_client_integration.post(f"/api/users/{user_b.user_id}/follow")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_followers_paginates(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    r = await async_client_integration.get(
        f"/api/users/{user_b.user_id}/followers?limit=1"
    )
    assert r.status_code == 200
    body = r.json()
    assert any(item["id"] == user_a.user_id for item in body["items"])
