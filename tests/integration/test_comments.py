"""Integration tests for comments router."""

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
async def test_create_and_list_comments(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)

    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "Qué bien se ve!"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 201
    created = r.json()
    assert created["body"] == "Qué bien se ve!"
    assert created["author"]["id"] == user_b.user_id

    r = await async_client_integration.get(
        f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_comment_permissions_flags(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    # user_b comments
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "from b"},
            cookies=user_b.cookies,
        )
    ).json()

    # user_a (not author of comment) sees can_delete=False, can_report=True
    items_a = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_a.cookies
        )
    ).json()["items"]
    mine_as_a = next(i for i in items_a if i["id"] == created["id"])
    assert mine_as_a["can_delete"] is False
    assert mine_as_a["can_report"] is True

    # user_b (author) sees can_delete=True, can_report=False
    items_b = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
        )
    ).json()["items"]
    mine_as_b = next(i for i in items_b if i["id"] == created["id"])
    assert mine_as_b["can_delete"] is True
    assert mine_as_b["can_report"] is False


@pytest.mark.asyncio
async def test_only_author_or_admin_can_delete(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "to delete"},
            cookies=user_b.cookies,
        )
    ).json()

    # user_a (not the commenter) → 403
    r = await async_client_integration.delete(
        f"/api/comments/{created['id']}", cookies=user_a.cookies
    )
    assert r.status_code == 403

    # user_b (commenter) → 204
    r = await async_client_integration.delete(
        f"/api/comments/{created['id']}", cookies=user_b.cookies
    )
    assert r.status_code == 204

    # Soft-deleted: list should not include it anymore.
    items = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
        )
    ).json()["items"]
    assert all(i["id"] != created["id"] for i in items)


@pytest.mark.asyncio
async def test_create_comment_on_missing_review_404(
    async_client_integration, user_a
):
    missing = uuid.uuid4()
    r = await async_client_integration.post(
        f"/api/reviews/{missing}/comments",
        json={"body": "ghost"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_comment_requires_auth(async_client_integration, user_a):
    review_id = await create_review(async_client_integration, user_a.cookies)
    async_client_integration.cookies.clear()
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments", json={"body": "hi"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_comment_generates_notification(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "buenísimo, voy mañana"},
        cookies=user_b.cookies,
    )
    notifs = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_a.cookies
        )
    ).json()["items"]
    assert any(
        n["kind"] == "comment" and n["target_review_id"] == review_id
        for n in notifs
    )
