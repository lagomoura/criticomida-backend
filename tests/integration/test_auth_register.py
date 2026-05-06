"""Integration tests against a real PostgreSQL (opt-in)."""

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


@pytest.mark.asyncio
async def test_register_returns_201_and_persists_handle(async_client_integration):
    email = f"pytest_{uuid.uuid4().hex}@example.com"
    handle = f"pytest_{uuid.uuid4().hex[:12]}"
    payload = {
        "email": email,
        "password": "longenough",
        "handle": handle,
    }

    response = await async_client_integration.post(
        "/api/auth/register",
        json=payload,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["email"] == email
    assert body["handle"] == handle.lower()
    # display_name is auto-populated from handle on creation so existing
    # FE components that render display_name keep working.
    assert body["display_name"] == handle.lower()


@pytest.mark.asyncio
async def test_register_rejects_taken_handle_case_insensitive(
    async_client_integration,
):
    handle = f"pytest_{uuid.uuid4().hex[:12]}"
    first = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": f"pytest_{uuid.uuid4().hex}@test.com",
            "password": "longenough",
            "handle": handle,
        },
    )
    assert first.status_code == 201, first.text

    # Mismo handle pero con mayúsculas → debe colisionar (CITEXT).
    second = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": f"pytest_{uuid.uuid4().hex}@test.com",
            "password": "longenough",
            "handle": handle.upper(),
        },
    )
    assert second.status_code == 409
    assert "username" in second.json()["detail"].lower()


@pytest.mark.asyncio
async def test_register_rejects_invalid_handle_format(async_client_integration):
    response = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": f"pytest_{uuid.uuid4().hex}@test.com",
            "password": "longenough",
            "handle": "ab",  # too short
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_handle_available_returns_true_for_free_handle(
    async_client_integration,
):
    handle = f"pytest_free_{uuid.uuid4().hex[:8]}"
    response = await async_client_integration.get(
        "/api/users/handle-available",
        params={"handle": handle},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["reason"] is None


@pytest.mark.asyncio
async def test_handle_available_returns_false_when_taken(
    async_client_integration,
):
    handle = f"pytest_{uuid.uuid4().hex[:12]}"
    reg = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": f"pytest_{uuid.uuid4().hex}@test.com",
            "password": "longenough",
            "handle": handle,
        },
    )
    assert reg.status_code == 201, reg.text

    # Buscar el mismo handle con otro casing — CITEXT debe matchear.
    response = await async_client_integration.get(
        "/api/users/handle-available",
        params={"handle": handle.upper()},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"] == "taken"


@pytest.mark.asyncio
async def test_handle_available_rejects_bad_format(async_client_integration):
    response = await async_client_integration.get(
        "/api/users/handle-available",
        params={"handle": "ab"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"] == "invalid_format"
