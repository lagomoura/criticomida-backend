"""Integration: registering a user notifies every admin (in-app).

Exercises the real wiring from `POST /api/auth/register` through
`notify_admins_user_created` into the `notifications` table, read back via
`GET /api/notifications` as the seeded admin.
"""

import os
import uuid

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_register_notifies_admin_in_app(
    async_client_integration, admin_client
):
    handle = f"pytest_{uuid.uuid4().hex[:12]}"
    email = f"pytest_{uuid.uuid4().hex[:10]}@test.com"

    reg = await async_client_integration.post(
        "/api/auth/register",
        json={"email": email, "password": "longenough", "handle": handle},
    )
    assert reg.status_code == 201, reg.text
    new_user_id = reg.json()["id"]

    items = (
        await async_client_integration.get(
            "/api/notifications", cookies=admin_client
        )
    ).json()["items"]

    match = next(
        (
            n
            for n in items
            if n["kind"] == "user_created"
            and n["target_user_id"] == new_user_id
        ),
        None,
    )
    assert match is not None, "admin did not receive a user_created notification"
    assert handle in match["text"]
    # El click debe poder llevar al perfil del usuario nuevo.
    assert match["target_user_id"] == new_user_id


@pytest.mark.asyncio
async def test_register_failure_does_not_emit_notification(
    async_client_integration, admin_client
):
    """A duplicate-handle 409 must not leave a stray user_created notif."""
    handle = f"pytest_{uuid.uuid4().hex[:12]}"

    first = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": f"pytest_{uuid.uuid4().hex[:10]}@test.com",
            "password": "longenough",
            "handle": handle,
        },
    )
    assert first.status_code == 201, first.text

    dup = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": f"pytest_{uuid.uuid4().hex[:10]}@test.com",
            "password": "longenough",
            "handle": handle,
        },
    )
    assert dup.status_code == 409, dup.text

    items = (
        await async_client_integration.get(
            "/api/notifications", cookies=admin_client
        )
    ).json()["items"]
    # Exactly one user_created notif mentions this handle (the successful one).
    hits = [
        n
        for n in items
        if n["kind"] == "user_created" and handle in n["text"]
    ]
    assert len(hits) == 1, f"expected 1 notif for {handle}, got {len(hits)}"
