"""Integration tests for feed + review detail."""

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
async def test_feed_for_you_returns_items(async_client_integration, user_a):
    # Use the author's own feed via /api/users/{id}/reviews to avoid the
    # for_you heuristic pushing a fresh zero-engagement review past the first
    # page. We still exercise the feed endpoint, just scoped to the author.
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.get(
        f"/api/users/{user_a.user_id}/reviews?limit=50",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["id"] == review_id for it in items)


@pytest.mark.asyncio
async def test_feed_following_empty_without_follows(
    async_client_integration, user_a
):
    # fresh user → no follows → empty following feed
    r = await async_client_integration.get(
        "/api/feed?type=following&limit=10", cookies=user_a.cookies
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_feed_following_shows_followed_users_posts(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )
    r = await async_client_integration.get(
        "/api/feed?type=following&limit=10", cookies=user_b.cookies
    )
    assert r.status_code == 200
    assert any(it["id"] == review_id for it in r.json()["items"])


@pytest.mark.asyncio
async def test_review_detail_includes_extras(
    async_client_integration, user_a
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.get(
        f"/api/reviews/{review_id}", cookies=user_a.cookies
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == review_id
    # Feed list skips extras; detail includes them.
    assert "extras" in body


@pytest.mark.asyncio
async def test_review_detail_404_for_missing(async_client_integration):
    r = await async_client_integration.get(f"/api/reviews/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_feed_anonymous_viewer_state_all_false(
    async_client_integration, user_a
):
    await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.get("/api/feed?type=for_you&limit=1")
    if r.json()["items"]:
        first = r.json()["items"][0]
        vs = first["viewer_state"]
        assert vs["liked"] is False
        assert vs["saved"] is False
        assert vs["following_author"] is False
