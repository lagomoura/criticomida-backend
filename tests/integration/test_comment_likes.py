"""Integration tests for /api/comments/{id}/like."""

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


async def _create_comment(client, review_id: str, cookies, body: str = "hola"):
    r = await client.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": body},
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_like_comment_increments_count(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = await _create_comment(
        async_client_integration, review_id, user_a.cookies, "padre"
    )

    r = await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )
    assert r.status_code == 200
    body = r.json()
    assert body["liked"] is True
    assert body["likes_count"] == 1
    assert body["comment_id"] == comment["id"]


@pytest.mark.asyncio
async def test_like_comment_idempotent(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = await _create_comment(
        async_client_integration, review_id, user_a.cookies
    )
    first = await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )
    second = await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )
    assert second.status_code == 200
    assert second.json()["likes_count"] == first.json()["likes_count"] == 1


@pytest.mark.asyncio
async def test_unlike_comment_removes(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = await _create_comment(
        async_client_integration, review_id, user_a.cookies
    )
    await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )
    r = await async_client_integration.delete(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )
    assert r.status_code == 200
    assert r.json()["liked"] is False
    assert r.json()["likes_count"] == 0


@pytest.mark.asyncio
async def test_like_missing_comment_404(async_client_integration, user_a):
    missing = uuid.uuid4()
    r = await async_client_integration.post(
        f"/api/comments/{missing}/like", cookies=user_a.cookies
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_like_soft_deleted_comment_404(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = await _create_comment(
        async_client_integration, review_id, user_b.cookies
    )
    await async_client_integration.delete(
        f"/api/comments/{comment['id']}", cookies=user_b.cookies
    )
    r = await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_a.cookies
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_comment_listing_reflects_likes(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = await _create_comment(
        async_client_integration, review_id, user_a.cookies
    )
    await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )

    items = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
        )
    ).json()["items"]
    mine = next(i for i in items if i["id"] == comment["id"])
    assert mine["likes_count"] == 1
    assert mine["viewer_liked"] is True

    # Otro viewer (autor del comment) no marcó like.
    items_a = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_a.cookies
        )
    ).json()["items"]
    mine_a = next(i for i in items_a if i["id"] == comment["id"])
    assert mine_a["likes_count"] == 1
    assert mine_a["viewer_liked"] is False


@pytest.mark.asyncio
async def test_comment_like_generates_notification(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = await _create_comment(
        async_client_integration, review_id, user_a.cookies, "ciclo de notif"
    )
    await async_client_integration.post(
        f"/api/comments/{comment['id']}/like", cookies=user_b.cookies
    )
    notifs = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_a.cookies
        )
    ).json()["items"]
    assert any(
        n["kind"] == "comment_like" and n["target_comment_id"] == comment["id"]
        for n in notifs
    )
