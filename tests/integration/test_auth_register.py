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
async def test_register_returns_201_and_persists_email(async_client_integration):
    email = f"pytest_{uuid.uuid4().hex}@example.com"
    payload = {
        "email": email,
        "password": "longenough",
        "display_name": "Pytest User",
    }

    response = await async_client_integration.post(
        "/api/auth/register",
        json=payload,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == email
    assert body["display_name"] == "Pytest User"
