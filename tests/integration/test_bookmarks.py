"""Integration tests for bookmarks router."""

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
async def test_save_and_list(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/save", cookies=user_b.cookies
    )
    assert r.status_code == 200
    assert r.json()["saved"] is True

    # /me/bookmarks now returns FeedPage; find our saved review in items.
    r = await async_client_integration.get(
        "/api/users/me/bookmarks", cookies=user_b.cookies
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(item["id"] == review_id for item in items)


@pytest.mark.asyncio
async def test_save_is_idempotent(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    first = await async_client_integration.post(
        f"/api/reviews/{review_id}/save", cookies=user_b.cookies
    )
    second = await async_client_integration.post(
        f"/api/reviews/{review_id}/save", cookies=user_b.cookies
    )
    assert first.json()["saves_count"] == second.json()["saves_count"]


@pytest.mark.asyncio
async def test_unsave_removes_from_list(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/save", cookies=user_b.cookies
    )
    r = await async_client_integration.delete(
        f"/api/reviews/{review_id}/save", cookies=user_b.cookies
    )
    assert r.status_code == 200
    assert r.json()["saved"] is False

    listing = await async_client_integration.get(
        "/api/users/me/bookmarks", cookies=user_b.cookies
    )
    assert all(it["id"] != review_id for it in listing.json()["items"])


@pytest.mark.asyncio
async def test_bookmarks_require_auth(async_client_integration):
    r = await async_client_integration.get("/api/users/me/bookmarks")
    assert r.status_code == 401
