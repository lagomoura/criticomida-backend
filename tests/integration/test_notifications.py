"""Integration tests for notifications router."""

import os

import pytest

from tests.integration.conftest import create_review

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_unread_count_starts_at_zero(async_client_integration, user_a):
    r = await async_client_integration.get(
        "/api/notifications/unread-count", cookies=user_a.cookies
    )
    assert r.status_code == 200
    assert r.json()["unread"] == 0


@pytest.mark.asyncio
async def test_unread_count_increments_on_like(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    r = await async_client_integration.get(
        "/api/notifications/unread-count", cookies=user_a.cookies
    )
    assert r.json()["unread"] >= 1


@pytest.mark.asyncio
async def test_mark_read_scoped_to_recipient(
    async_client_integration, user_a, user_b
):
    """user_b cannot mark user_a's notification as read."""
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    notifs = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_a.cookies
        )
    ).json()["items"]
    target = next(n for n in notifs if n["target_review_id"] == review_id)

    # user_b tries to mark user_a's notification → 404 (scoped query).
    r = await async_client_integration.post(
        f"/api/notifications/{target['id']}/read", cookies=user_b.cookies
    )
    assert r.status_code == 404

    # user_a can mark it.
    r = await async_client_integration.post(
        f"/api/notifications/{target['id']}/read", cookies=user_a.cookies
    )
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_mark_all_read(async_client_integration, user_a, user_b):
    review1 = await create_review(async_client_integration, user_a.cookies)
    review2 = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review1}/like", cookies=user_b.cookies
    )
    await async_client_integration.post(
        f"/api/reviews/{review2}/like", cookies=user_b.cookies
    )

    r = await async_client_integration.post(
        "/api/notifications/read-all", cookies=user_a.cookies
    )
    assert r.status_code == 204

    count = await async_client_integration.get(
        "/api/notifications/unread-count", cookies=user_a.cookies
    )
    assert count.json()["unread"] == 0


@pytest.mark.asyncio
async def test_list_requires_auth(async_client_integration):
    r = await async_client_integration.get("/api/notifications")
    assert r.status_code == 401
