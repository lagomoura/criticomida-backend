"""Integration tests for /api/reviews/{id}/like."""

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
async def test_like_increments_count(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    assert r.status_code == 200
    body = r.json()
    assert body["liked"] is True
    assert body["likes_count"] == 1


@pytest.mark.asyncio
async def test_like_is_idempotent(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    first = await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    second = await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    assert second.status_code == 200
    assert second.json()["likes_count"] == first.json()["likes_count"]


@pytest.mark.asyncio
async def test_unlike_removes(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    r = await async_client_integration.delete(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    assert r.status_code == 200
    assert r.json()["liked"] is False
    assert r.json()["likes_count"] == 0


@pytest.mark.asyncio
async def test_like_nonexistent_review_404(async_client_integration, user_a):
    missing = uuid.uuid4()
    r = await async_client_integration.post(
        f"/api/reviews/{missing}/like", cookies=user_a.cookies
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_like_requires_auth(async_client_integration, user_a):
    review_id = await create_review(async_client_integration, user_a.cookies)
    async_client_integration.cookies.clear()
    r = await async_client_integration.post(f"/api/reviews/{review_id}/like")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_like_generates_notification_for_author(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    # Author should see one unread notification of kind 'like'.
    notifs = await async_client_integration.get(
        "/api/notifications", cookies=user_a.cookies
    )
    assert notifs.status_code == 200
    items = notifs.json()["items"]
    assert any(
        n["kind"] == "like" and n["target_review_id"] == review_id for n in items
    )


@pytest.mark.asyncio
async def test_self_like_does_not_generate_notification(
    async_client_integration, user_a
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_a.cookies
    )
    notifs = await async_client_integration.get(
        "/api/notifications", cookies=user_a.cookies
    )
    for n in notifs.json()["items"]:
        assert not (
            n["kind"] == "like"
            and n["target_review_id"] == review_id
            and n["actor"]["id"] == user_a.user_id
        )
