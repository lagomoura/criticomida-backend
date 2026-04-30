"""Integration tests for reservation affiliate fields + click logging."""

import os
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.database import engine

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _create_resto_with_reservation(
    client, admin_cookies, *, with_url: bool
) -> dict:
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    payload = {
        "slug": "",
        "name": f"Resto Reservas {uuid.uuid4().hex[:6]}",
        "location_name": "Av. Test 1234, CABA",
        "google_place_id": place_id,
    }
    if with_url:
        payload["reservation_url"] = "https://wa.me/5491133334444"
        payload["reservation_provider"] = "whatsapp"

    r = await client.post("/api/restaurants", json=payload, cookies=admin_cookies)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_create_persists_reservation_fields(
    async_client_integration, admin_client
):
    body = await _create_resto_with_reservation(
        async_client_integration, admin_client, with_url=True
    )
    assert body["reservation_url"] == "https://wa.me/5491133334444"
    assert body["reservation_provider"] == "whatsapp"


@pytest.mark.asyncio
async def test_list_exposes_has_reservation(
    async_client_integration, admin_client
):
    body = await _create_resto_with_reservation(
        async_client_integration, admin_client, with_url=True
    )

    r = await async_client_integration.get("/api/restaurants?per_page=100")
    assert r.status_code == 200
    items = r.json()["items"]
    match = next((i for i in items if i["slug"] == body["slug"]), None)
    assert match is not None
    assert match["has_reservation"] is True
    assert match["reservation_provider"] == "whatsapp"


@pytest.mark.asyncio
async def test_click_anonymous_returns_204(
    async_client_integration, admin_client, integration_app
):
    body = await _create_resto_with_reservation(
        async_client_integration, admin_client, with_url=True
    )

    # Use a fresh client without any sticky cookies to assert anonymous logging.
    transport = httpx.ASGITransport(app=integration_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as anon:
        r = await anon.post(
            f"/api/restaurants/{body['slug']}/reservation-click",
            json={
                "provider": "whatsapp",
                "utm": {"utm_source": "criticomida"},
                "session_id": "pytest-sess",
            },
        )
    assert r.status_code == 204

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT user_id, provider, session_id, utm "
                    "FROM reservation_clicks "
                    "WHERE restaurant_id = :rid"
                ),
                {"rid": body["id"]},
            )
        ).first()
    assert row is not None
    assert row.user_id is None
    assert row.provider == "whatsapp"
    assert row.session_id == "pytest-sess"
    assert row.utm == {"utm_source": "criticomida"}


@pytest.mark.asyncio
async def test_click_without_reservation_url_returns_404(
    async_client_integration, admin_client
):
    body = await _create_resto_with_reservation(
        async_client_integration, admin_client, with_url=False
    )

    r = await async_client_integration.post(
        f"/api/restaurants/{body['slug']}/reservation-click",
        json={},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_click_unknown_slug_returns_404(async_client_integration):
    r = await async_client_integration.post(
        "/api/restaurants/zzz-no-existe-pytest/reservation-click",
        json={},
    )
    assert r.status_code == 404
