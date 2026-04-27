import os
import uuid
from typing import Any, NamedTuple

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from app.database import engine
from app.main import create_app
from app.middleware.rate_limit import limiter as _rate_limiter


@pytest.fixture(scope="session", autouse=True)
def _disable_rate_limiter():
    """Keep slowapi out of the way for tests that exercise many actions.

    The dedicated rate-limit test module re-enables it locally.
    """
    _rate_limiter.enabled = False
    yield


@pytest.fixture(scope="session", autouse=True)
async def _truncate_pytest_data():
    """Leave the DB clean at the end of an integration run.

    Users/restaurants created by these tests use well-known patterns
    (`pytest_%@test.com`, `pytest_place_%`). Order matters: `restaurants`
    has `created_by -> users.id` without CASCADE, so restaurants must be
    deleted first (that cascades dishes/reviews/etc.), then users (cascades
    follows/likes/comments/bookmarks/notifications/refresh_tokens).
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM restaurants WHERE google_place_id LIKE 'pytest_place_%' "
                "OR created_by IN (SELECT id FROM users WHERE email LIKE 'pytest_%@test.com')"
            )
        )
        await conn.execute(
            text("DELETE FROM users WHERE email LIKE 'pytest_%@test.com'")
        )


@pytest.fixture
def integration_app() -> FastAPI:
    return create_app()


@pytest.fixture
async def async_client_integration(integration_app: FastAPI):
    transport = httpx.ASGITransport(app=integration_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        yield client


class RegisteredUser(NamedTuple):
    email: str
    user_id: str
    cookies: Any


async def register_and_login(
    client: httpx.AsyncClient,
    *,
    password: str = "longenough",
    display_name: str | None = None,
) -> RegisteredUser:
    """Register a random user and return an authenticated-cookies bundle.

    Each call creates a fresh user — tests using this don't interfere.
    """
    email = f"pytest_{uuid.uuid4().hex[:10]}@test.com"
    reg = await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": password,
            "display_name": display_name or f"User {uuid.uuid4().hex[:6]}",
        },
    )
    assert reg.status_code == 201, reg.text
    user_id = reg.json()["id"]

    login = await client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return RegisteredUser(email=email, user_id=user_id, cookies=login.cookies)


async def create_review(
    client: httpx.AsyncClient,
    cookies: Any,
    *,
    place_id: str | None = None,
    restaurant_name: str = "Test Pytest Resto",
    dish_name: str = "Plato de prueba",
    score: float = 4.0,
    text: str = "Review de prueba generada por integration tests.",
    city: str | None = "Buenos Aires",
) -> str:
    """POST /api/posts and return the created review's id.

    Uses a fresh place_id per call unless caller overrides, so two reviews by
    the same user never collide on unique(dish_id, user_id).
    """
    pid = place_id or f"pytest_place_{uuid.uuid4().hex[:10]}"
    payload: dict[str, Any] = {
        "restaurant": {
            "place_id": pid,
            "name": restaurant_name,
            "formatted_address": f"{restaurant_name}, {city or 'BA'}",
            "city": city,
            "latitude": -34.6,
            "longitude": -58.4,
        },
        "dish_name": dish_name,
        "score": score,
        "text": text,
    }
    r = await client.post("/api/posts", json=payload, cookies=cookies)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def user_a(async_client_integration) -> RegisteredUser:
    return await register_and_login(async_client_integration)


@pytest.fixture
async def user_b(async_client_integration) -> RegisteredUser:
    return await register_and_login(async_client_integration)


@pytest.fixture
async def admin_client(async_client_integration):
    """Logs in as the seeded admin account.

    The seed user in dev is `admin@criticomida.com` / `admin123`. Override via
    env vars `INTEGRATION_ADMIN_EMAIL` / `INTEGRATION_ADMIN_PASSWORD` when the
    seed differs (CI, staging).
    """
    email = os.environ.get("INTEGRATION_ADMIN_EMAIL", "admin@criticomida.com")
    password = os.environ.get("INTEGRATION_ADMIN_PASSWORD", "admin123")
    r = await async_client_integration.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    if r.status_code != 200:
        pytest.skip(f"Admin seed user {email} not available ({r.status_code}).")
    return r.cookies
