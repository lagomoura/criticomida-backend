"""Integration tests for the users router (PR 1: profile fields)."""

import os
import uuid

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 and a reachable DATABASE_URL "
        "to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _register_and_login(client) -> tuple[str, dict]:
    email = f"pytest_{uuid.uuid4().hex}@example.com"
    payload = {
        "email": email,
        "password": "longenough",
        "display_name": "Pytest User",
    }
    reg = await client.post("/api/auth/register", json=payload)
    assert reg.status_code == 201
    login = await client.post(
        "/api/auth/login", json={"email": email, "password": "longenough"}
    )
    assert login.status_code == 200
    return email, login.cookies


@pytest.mark.asyncio
async def test_update_profile_sets_handle_bio_location(async_client_integration):
    _, cookies = await _register_and_login(async_client_integration)

    unique_handle = f"user_{uuid.uuid4().hex[:8]}"
    response = await async_client_integration.patch(
        "/api/users/me",
        json={
            "handle": unique_handle,
            "bio": "Pruebo platos, reseño, repito.",
            "location": "Buenos Aires",
        },
        cookies=cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["handle"] == unique_handle.lower()
    assert body["bio"] == "Pruebo platos, reseño, repito."
    assert body["location"] == "Buenos Aires"


@pytest.mark.asyncio
async def test_update_profile_rejects_duplicate_handle(async_client_integration):
    _, cookies_a = await _register_and_login(async_client_integration)
    shared_handle = f"dup_{uuid.uuid4().hex[:8]}"
    first = await async_client_integration.patch(
        "/api/users/me",
        json={"handle": shared_handle},
        cookies=cookies_a,
    )
    assert first.status_code == 200

    _, cookies_b = await _register_and_login(async_client_integration)
    second = await async_client_integration.patch(
        "/api/users/me",
        json={"handle": shared_handle},
        cookies=cookies_b,
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_public_profile_by_uuid(async_client_integration):
    _, cookies = await _register_and_login(async_client_integration)
    me = await async_client_integration.get("/api/auth/me", cookies=cookies)
    user_id = me.json()["id"]

    response = await async_client_integration.get(f"/api/users/{user_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == user_id
    assert body["counts"] == {"reviews": 0, "followers": 0, "following": 0}


@pytest.mark.asyncio
async def test_public_profile_by_handle(async_client_integration):
    _, cookies = await _register_and_login(async_client_integration)
    handle = f"findme_{uuid.uuid4().hex[:8]}"
    await async_client_integration.patch(
        "/api/users/me",
        json={"handle": handle},
        cookies=cookies,
    )

    response = await async_client_integration.get(f"/api/users/{handle}")
    assert response.status_code == 200
    assert response.json()["handle"] == handle.lower()


@pytest.mark.asyncio
async def test_public_profile_not_found(async_client_integration):
    missing = f"ghost_{uuid.uuid4().hex[:8]}"
    response = await async_client_integration.get(f"/api/users/{missing}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_requires_auth(async_client_integration):
    response = await async_client_integration.patch(
        "/api/users/me",
        json={"bio": "nope"},
    )
    assert response.status_code == 401
