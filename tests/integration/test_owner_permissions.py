"""Integration tests for verified-owner permissions (Hito 6)."""

import os
import uuid

import pytest

from app.database import engine
from sqlalchemy import text

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _seed_resto_with_review(client, admin_cookies, user) -> tuple[dict, str]:
    """Crea un resto + un dish + una review del user. Devuelve (resto, review_id)."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    create = await client.post(
        "/api/restaurants",
        json={
            "slug": "",
            "name": f"Resto Owner {uuid.uuid4().hex[:6]}",
            "location_name": "Av. Test 1, CABA",
            "google_place_id": place_id,
        },
        cookies=admin_cookies,
    )
    assert create.status_code == 201, create.text
    resto = create.json()

    # Postear una review usa el flow alto-nivel /api/posts.
    post = await client.post(
        "/api/posts",
        json={
            "restaurant": {
                "place_id": place_id,
                "name": resto["name"],
                "formatted_address": resto["location_name"],
                "city": "Buenos Aires",
                "latitude": -34.6,
                "longitude": -58.4,
            },
            "dish_name": "Plato Owner",
            "score": 4.0,
            "text": "Reseña base para los tests de owner-response.",
        },
        cookies=user.cookies,
    )
    assert post.status_code == 201, post.text
    return resto, post.json()["id"]


async def _make_owner(resto_id: str, user_id: str) -> None:
    """Atajo: en vez de pasar por el flow del claim, marcamos al user como
    owner directo en DB (las pruebas del claim ya cubren ese flow)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE restaurants SET claimed_by_user_id = :uid, "
                "claimed_at = now() WHERE id = :rid"
            ),
            {"uid": user_id, "rid": resto_id},
        )


# ── Owner response a una review ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_owner_can_respond_to_review(
    async_client_integration, admin_client, user_a, user_b
):
    resto, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    # user_b no es owner → 403.
    forbidden = await async_client_integration.put(
        f"/api/dish-reviews/{review_id}/owner-response",
        json={"body": "Soy un impostor"},
        cookies=user_b.cookies,
    )
    assert forbidden.status_code == 403

    # user_a (owner) → 200.
    ok = await async_client_integration.put(
        f"/api/dish-reviews/{review_id}/owner-response",
        json={"body": "Gracias por la visita"},
        cookies=user_a.cookies,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["body"] == "Gracias por la visita"
    assert body["owner_user_id"] == user_a.user_id


@pytest.mark.asyncio
async def test_owner_response_is_idempotent_and_editable(
    async_client_integration, admin_client, user_a
):
    resto, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    first = await async_client_integration.put(
        f"/api/dish-reviews/{review_id}/owner-response",
        json={"body": "Primera versión"},
        cookies=user_a.cookies,
    )
    assert first.status_code == 200

    second = await async_client_integration.put(
        f"/api/dish-reviews/{review_id}/owner-response",
        json={"body": "Editado"},
        cookies=user_a.cookies,
    )
    assert second.status_code == 200
    assert second.json()["body"] == "Editado"

    # Solo una fila por review en DB.
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM dish_review_owner_responses "
                    "WHERE review_id = :rid"
                ),
                {"rid": review_id},
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_public_can_read_owner_response(
    async_client_integration, admin_client, user_a
):
    resto, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)
    await async_client_integration.put(
        f"/api/dish-reviews/{review_id}/owner-response",
        json={"body": "Respuesta pública"},
        cookies=user_a.cookies,
    )

    import httpx
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as anon:
        r = await anon.get(f"/api/dish-reviews/{review_id}/owner-response")
    assert r.status_code == 200
    assert r.json()["body"] == "Respuesta pública"


@pytest.mark.asyncio
async def test_owner_can_delete_response(
    async_client_integration, admin_client, user_a
):
    resto, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)
    await async_client_integration.put(
        f"/api/dish-reviews/{review_id}/owner-response",
        json={"body": "Borrar"},
        cookies=user_a.cookies,
    )

    deleted = await async_client_integration.delete(
        f"/api/dish-reviews/{review_id}/owner-response",
        cookies=user_a.cookies,
    )
    assert deleted.status_code == 204

    after = await async_client_integration.get(
        f"/api/dish-reviews/{review_id}/owner-response"
    )
    assert after.status_code == 200
    assert after.json() is None


# ── Fotos oficiales del restaurant ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_owner_can_add_official_photo(
    async_client_integration, admin_client, user_a, user_b
):
    resto, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    forbidden = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/official-photos",
        json={"url": "/uploads/foo.jpg"},
        cookies=user_b.cookies,
    )
    assert forbidden.status_code == 403

    ok = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/official-photos",
        json={"url": "/uploads/foo.jpg", "alt_text": "Frente"},
        cookies=user_a.cookies,
    )
    assert ok.status_code == 201
    assert ok.json()["url"] == "/uploads/foo.jpg"


@pytest.mark.asyncio
async def test_official_photos_are_capped_at_five(
    async_client_integration, admin_client, user_a
):
    resto, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    for i in range(5):
        r = await async_client_integration.post(
            f"/api/restaurants/{resto['slug']}/official-photos",
            json={"url": f"/uploads/p{i}.jpg"},
            cookies=user_a.cookies,
        )
        assert r.status_code == 201, r.text

    overflow = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/official-photos",
        json={"url": "/uploads/p6.jpg"},
        cookies=user_a.cookies,
    )
    assert overflow.status_code == 409


@pytest.mark.asyncio
async def test_owner_can_delete_official_photo(
    async_client_integration, admin_client, user_a
):
    resto, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    create = await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/official-photos",
        json={"url": "/uploads/x.jpg"},
        cookies=user_a.cookies,
    )
    photo_id = create.json()["id"]

    deleted = await async_client_integration.delete(
        f"/api/restaurants/{resto['slug']}/official-photos/{photo_id}",
        cookies=user_a.cookies,
    )
    assert deleted.status_code == 204

    after = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}/official-photos"
    )
    assert after.status_code == 200
    assert after.json()["items"] == []


@pytest.mark.asyncio
async def test_public_lists_official_photos(
    async_client_integration, admin_client, user_a
):
    resto, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)
    await async_client_integration.post(
        f"/api/restaurants/{resto['slug']}/official-photos",
        json={"url": "/uploads/visible.jpg"},
        cookies=user_a.cookies,
    )

    # Cliente anónimo (sin cookies sticky).
    import httpx
    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as anon:
        r = await anon.get(f"/api/restaurants/{resto['slug']}/official-photos")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["url"] == "/uploads/visible.jpg"
