"""Integration tests for the restaurant claim flow (Hito 3)."""

import os
import uuid

import pytest
from sqlalchemy import text

from app.database import engine

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _create_resto(client, admin_cookies) -> dict:
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    payload = {
        "slug": "",
        "name": f"Resto Claim {uuid.uuid4().hex[:6]}",
        "location_name": "Av. Test 1234, CABA",
        "google_place_id": place_id,
    }
    r = await client.post(
        "/api/restaurants", json=payload, cookies=admin_cookies
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _read_email_token(claim_id: str) -> str | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT verification_payload->>'email_token' "
                    "FROM restaurant_claims WHERE id = :id"
                ),
                {"id": claim_id},
            )
        ).first()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_create_claim_pending(async_client_integration, admin_client, user_a):
    resto = await _create_resto(async_client_integration, admin_client)

    r = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={
            "verification_method": "manual_admin",
            "evidence_urls": ["https://example.com/cartel.jpg"],
        },
        cookies=user_a.cookies,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["verification_method"] == "manual_admin"
    assert body["evidence_urls"] == ["https://example.com/cartel.jpg"]


@pytest.mark.asyncio
async def test_create_claim_requires_auth(async_client_integration, admin_client):
    resto = await _create_resto(async_client_integration, admin_client)
    # Build a clean client to avoid sticky admin cookies.
    import httpx
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        r = await anon.post(
            f"/api/restaurants/{resto['slug']}/claims",
            json={"verification_method": "manual_admin"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_claim_duplicate_open_returns_409(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    first = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={"verification_method": "manual_admin"},
        cookies=user_a.cookies,
    )
    assert first.status_code == 201

    second = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={"verification_method": "manual_admin"},
        cookies=user_a.cookies,
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_domain_email_requires_contact_email(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    r = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={"verification_method": "domain_email"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_verify_email_token_auto_approves(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    create = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={
            "verification_method": "domain_email",
            "contact_email": "owner@example.com",
        },
        cookies=user_a.cookies,
    )
    assert create.status_code == 201
    claim_id = create.json()["id"]

    token = await _read_email_token(claim_id)
    assert token and len(token) >= 32

    verify = await async_client_integration.post(
        f"/api/claims/verify-email-token/{token}"
    )
    assert verify.status_code == 200
    body = verify.json()
    assert body["status"] == "verified"
    assert body["reviewed_at"] is not None

    status_resp = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}/claim-status"
    )
    assert status_resp.status_code == 200
    assert status_resp.json() == {"is_claimed": True}

    # is_claimed se expone también en el detail endpoint.
    detail = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}"
    )
    assert detail.status_code == 200
    assert detail.json()["is_claimed"] is True


@pytest.mark.asyncio
async def test_verify_email_token_idempotent(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    create = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={
            "verification_method": "domain_email",
            "contact_email": "owner@example.com",
        },
        cookies=user_a.cookies,
    )
    claim_id = create.json()["id"]
    token = await _read_email_token(claim_id)

    first = await async_client_integration.post(
        f"/api/claims/verify-email-token/{token}"
    )
    assert first.status_code == 200

    # Second call with the same token should still resolve the (now-verified)
    # claim without flipping state. The token has already been rotated out of
    # verification_payload, so a second hit should now 404.
    second = await async_client_integration.post(
        f"/api/claims/verify-email-token/{token}"
    )
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_cannot_claim_already_verified_restaurant(
    async_client_integration, admin_client, user_a, user_b
):
    resto = await _create_resto(async_client_integration, admin_client)
    create = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={
            "verification_method": "domain_email",
            "contact_email": "owner@example.com",
        },
        cookies=user_a.cookies,
    )
    claim_id = create.json()["id"]
    token = await _read_email_token(claim_id)
    await async_client_integration.post(
        f"/api/claims/verify-email-token/{token}"
    )

    # user_b tries to claim the same restaurant — must 409.
    rival = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={"verification_method": "manual_admin"},
        cookies=user_b.cookies,
    )
    assert rival.status_code == 409


@pytest.mark.asyncio
async def test_claim_status_unknown_slug(async_client_integration):
    r = await async_client_integration.get(
        "/api/restaurants/zzz-no-existe-pytest/claim-status"
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_my_claims_returns_user_only(
    async_client_integration, admin_client, user_a, user_b
):
    resto = await _create_resto(async_client_integration, admin_client)
    await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={"verification_method": "manual_admin"},
        cookies=user_a.cookies,
    )

    a_claims = await async_client_integration.get(
        "/api/me/claims", cookies=user_a.cookies
    )
    assert a_claims.status_code == 200
    assert len(a_claims.json()["items"]) >= 1

    b_claims = await async_client_integration.get(
        "/api/me/claims", cookies=user_b.cookies
    )
    assert b_claims.status_code == 200
    # user_b nunca creó claims — no ve los de user_a.
    assert all(
        item["claimant_user_id"] == user_b.user_id
        for item in b_claims.json()["items"]
    )
