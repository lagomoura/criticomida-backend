"""Integration tests for the reports (moderation) router."""

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
async def test_user_can_create_report(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.post(
        "/api/reports",
        json={
            "entity_type": "review",
            "entity_id": review_id,
            "reason": "Pytest test report",
        },
        cookies=user_b.cookies,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["entity_id"] == review_id


@pytest.mark.asyncio
async def test_create_requires_auth(async_client_integration):
    r = await async_client_integration.post(
        "/api/reports",
        json={
            "entity_type": "review",
            "entity_id": "00000000-0000-0000-0000-000000000000",
            "reason": "x",
        },
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_requires_admin(async_client_integration, user_a):
    r = await async_client_integration.get(
        "/api/reports?status=pending", cookies=user_a.cookies
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_list_and_patch(
    async_client_integration, user_a, user_b, admin_client
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    create = await async_client_integration.post(
        "/api/reports",
        json={
            "entity_type": "review",
            "entity_id": review_id,
            "reason": "Test for admin pass",
        },
        cookies=user_b.cookies,
    )
    report_id = create.json()["id"]

    # Admin can list pending.
    listing = await async_client_integration.get(
        "/api/reports?status=pending", cookies=admin_client
    )
    assert listing.status_code == 200
    assert any(it["id"] == report_id for it in listing.json()["items"])

    # Admin can change status.
    patch = await async_client_integration.patch(
        f"/api/reports/{report_id}",
        json={"status": "reviewed"},
        cookies=admin_client,
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "reviewed"


@pytest.mark.asyncio
async def test_admin_comment_report_hydrates_parent_id(
    async_client_integration, user_a, user_b, admin_client
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    comment = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "por reportar"},
            cookies=user_b.cookies,
        )
    ).json()

    # user_a reports user_b's comment.
    await async_client_integration.post(
        "/api/reports",
        json={
            "entity_type": "comment",
            "entity_id": comment["id"],
            "reason": "Bad comment",
        },
        cookies=user_a.cookies,
    )

    listing = (
        await async_client_integration.get(
            "/api/reports?status=pending", cookies=admin_client
        )
    ).json()["items"]
    match = next(
        it
        for it in listing
        if it["entity_type"] == "comment" and it["entity_id"] == comment["id"]
    )
    assert match["target"]["parent_id"] == review_id
