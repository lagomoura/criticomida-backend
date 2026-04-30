"""Integration tests for the admin claim review endpoints (Hito 4)."""

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
        "name": f"Resto Admin {uuid.uuid4().hex[:6]}",
        "location_name": "Av. Test 1234, CABA",
        "google_place_id": place_id,
    }
    r = await client.post("/api/restaurants", json=payload, cookies=admin_cookies)
    assert r.status_code == 201, r.text
    return r.json()


async def _open_claim(client, resto, user) -> str:
    r = await client.post(
        f"/api/restaurants/{resto['slug']}/claims",
        json={"verification_method": "manual_admin"},
        cookies=user.cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_admin_list_filters_by_status(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    await _open_claim(async_client_integration, resto, user_a)

    r = await async_client_integration.get(
        "/api/admin/claims?status=pending", cookies=admin_client
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert all(item["status"] == "pending" for item in body["items"])
    # Hidratación: cada item incluye restaurant + claimant.
    item = next(i for i in body["items"] if i["restaurant"]["slug"] == resto["slug"])
    assert item["restaurant"]["name"] == resto["name"]
    assert item["claimant"]["email"] == user_a.email


@pytest.mark.asyncio
async def test_admin_list_requires_admin(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    await _open_claim(async_client_integration, resto, user_a)

    r = await async_client_integration.get(
        "/api/admin/claims", cookies=user_a.cookies
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_approve_sets_owner(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    r = await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/approve",
        json={"notes": "Verified offline"},
        cookies=admin_client,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "verified"
    assert body["restaurant"]["is_claimed"] is True

    detail = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}"
    )
    assert detail.json()["is_claimed"] is True


@pytest.mark.asyncio
async def test_admin_reject_requires_reason(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    # Empty body → 422 validation error.
    bad = await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/reject",
        json={},
        cookies=admin_client,
    )
    assert bad.status_code == 422

    ok = await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/reject",
        json={"reason": "Evidence insufficient"},
        cookies=admin_client,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "rejected"
    assert body["rejection_reason"] == "Evidence insufficient"


@pytest.mark.asyncio
async def test_admin_revoke_clears_owner(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/approve",
        json={},
        cookies=admin_client,
    )

    revoke = await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/revoke",
        json={"reason": "Owner is not the real owner"},
        cookies=admin_client,
    )
    assert revoke.status_code == 200
    body = revoke.json()
    assert body["status"] == "revoked"
    assert body["restaurant"]["is_claimed"] is False

    # Detail endpoint también vuelve a is_claimed=false.
    detail = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}"
    )
    assert detail.json()["is_claimed"] is False


@pytest.mark.asyncio
async def test_admin_approve_already_rejected_returns_409(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/reject",
        json={"reason": "Evidence insufficient"},
        cookies=admin_client,
    )

    r = await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/approve",
        json={},
        cookies=admin_client,
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_admin_revoke_pending_returns_409(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    r = await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/revoke",
        json={"reason": "Some reason"},
        cookies=admin_client,
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_admin_endpoints_404_on_unknown_claim(
    async_client_integration, admin_client
):
    bogus = uuid.uuid4()
    r = await async_client_integration.post(
        f"/api/admin/claims/{bogus}/approve",
        json={},
        cookies=admin_client,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_approve_creates_in_app_notification(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/approve",
        json={},
        cookies=admin_client,
    )

    notifs = await async_client_integration.get(
        "/api/notifications", cookies=user_a.cookies
    )
    assert notifs.status_code == 200
    items = notifs.json()["items"]
    approved = next(
        (i for i in items if i["kind"] == "claim_approved"), None
    )
    assert approved is not None
    assert approved["target_restaurant_id"] == resto["id"]
    assert resto["name"] in approved["text"]


@pytest.mark.asyncio
async def test_reject_creates_in_app_notification_with_reason(
    async_client_integration, admin_client, user_a
):
    resto = await _create_resto(async_client_integration, admin_client)
    claim_id = await _open_claim(async_client_integration, resto, user_a)

    await async_client_integration.post(
        f"/api/admin/claims/{claim_id}/reject",
        json={"reason": "evidencia insuficiente"},
        cookies=admin_client,
    )

    notifs = await async_client_integration.get(
        "/api/notifications", cookies=user_a.cookies
    )
    items = notifs.json()["items"]
    rejected = next(
        (i for i in items if i["kind"] == "claim_rejected"), None
    )
    assert rejected is not None
    assert "evidencia insuficiente" in rejected["text"]


@pytest.mark.asyncio
async def test_revoke_only_clears_owner_if_active(
    async_client_integration, admin_client, user_a, user_b
):
    """Si user_a's claim es revocado *después* de que se transfirió la
    propiedad a user_b (futuro feature), el revoke no debe pisarle el
    owner. Validamos directo en DB porque no hay endpoint de transferencia
    todavía."""
    resto = await _create_resto(async_client_integration, admin_client)
    claim_a = await _open_claim(async_client_integration, resto, user_a)

    await async_client_integration.post(
        f"/api/admin/claims/{claim_a}/approve",
        json={},
        cookies=admin_client,
    )

    # Forzar manualmente otro owner (simulando transferencia administrativa).
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE restaurants SET claimed_by_user_id = :uid WHERE id = :rid"
            ),
            {"uid": user_b.user_id, "rid": resto["id"]},
        )

    revoke = await async_client_integration.post(
        f"/api/admin/claims/{claim_a}/revoke",
        json={"reason": "Re-evaluación"},
        cookies=admin_client,
    )
    assert revoke.status_code == 200

    # El owner sigue siendo user_b — el revoke solo limpia si era ese claim.
    detail = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}"
    )
    assert detail.json()["is_claimed"] is True
