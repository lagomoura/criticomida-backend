"""Integration tests para el flujo "owner asigna foto oficial al plato".

Cubre los tres endpoints del router ``owner_dishes``:

- ``PUT  /api/restaurants/{slug}/dishes/{dish_id}/cover``
- ``DELETE /api/restaurants/{slug}/dishes/{dish_id}/cover``
- ``GET  /api/restaurants/{slug}/dishes/{dish_id}/photo-candidates``

Sigue el patrón de ``test_owner_permissions``: marca al user como owner via
SQL directo (los tests del claim ya cubren ese flow) y usa URLs fake
para las fotos — los endpoints solo manipulan strings de URL, la subida
real va por ``/api/images/upload`` cuyo flujo está cubierto en otros tests.
"""

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


async def _seed_resto_with_review(client, admin_cookies, user) -> tuple[dict, str, str]:
    """Crea resto + dish + review del user. Devuelve (resto, dish_id, review_id)."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    create = await client.post(
        "/api/restaurants",
        json={
            "slug": "",
            "name": f"Resto Cover {uuid.uuid4().hex[:6]}",
            "location_name": "Av. Test 1, CABA",
            "google_place_id": place_id,
        },
        cookies=admin_cookies,
    )
    assert create.status_code == 201, create.text
    resto = create.json()

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
            "dish_name": f"Plato Cover {uuid.uuid4().hex[:4]}",
            "score": 4.5,
            "text": "Reseña base para los tests de cover oficial.",
        },
        cookies=user.cookies,
    )
    assert post.status_code == 201, post.text
    review_id = post.json()["id"]

    # Recuperar el dish_id que la review creó (camino /api/posts).
    async with engine.connect() as conn:
        dish_id = (
            await conn.execute(
                text("SELECT dish_id FROM dish_reviews WHERE id = :rid"),
                {"rid": review_id},
            )
        ).scalar_one()
    return resto, str(dish_id), review_id


async def _make_owner(resto_id: str, user_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE restaurants SET claimed_by_user_id = :uid, "
                "claimed_at = now() WHERE id = :rid"
            ),
            {"uid": user_id, "rid": resto_id},
        )


async def _insert_review_image(
    review_id: str, url: str, *, display_order: int = 0, alt_text: str | None = None
) -> str:
    image_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dish_review_images "
                "(id, dish_review_id, url, alt_text, display_order, uploaded_at) "
                "VALUES (:id, :rid, :url, :alt, :ord, now())"
            ),
            {
                "id": image_id,
                "rid": review_id,
                "url": url,
                "alt": alt_text,
                "ord": display_order,
            },
        )
    return image_id


async def _read_dish_cover(dish_id: str) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT cover_image_url FROM dishes WHERE id = :id"),
                {"id": dish_id},
            )
        ).scalar_one()


# ── Set cover ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_owner_can_set_dish_cover(
    async_client_integration, admin_client, user_a, user_b
):
    resto, dish_id, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    forbidden = await async_client_integration.put(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/cover",
        json={"url": "/uploads/intruso.jpg"},
        cookies=user_b.cookies,
    )
    assert forbidden.status_code == 403

    ok = await async_client_integration.put(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/cover",
        json={"url": "/uploads/oficial.jpg"},
        cookies=user_a.cookies,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["dish_id"] == dish_id
    assert body["cover_image_url"] == "/uploads/oficial.jpg"
    assert await _read_dish_cover(dish_id) == "/uploads/oficial.jpg"


@pytest.mark.asyncio
async def test_set_cover_overwrites_previous_value(
    async_client_integration, admin_client, user_a
):
    """Idempotente: la segunda llamada pisa la primera (el owner siempre gana,
    incluyendo sobre auto-asignaciones del cron)."""
    resto, dish_id, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    for url in ("/uploads/v1.jpg", "/uploads/v2.jpg"):
        r = await async_client_integration.put(
            f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/cover",
            json={"url": url},
            cookies=user_a.cookies,
        )
        assert r.status_code == 200, r.text
    assert await _read_dish_cover(dish_id) == "/uploads/v2.jpg"


@pytest.mark.asyncio
async def test_cannot_set_cover_for_dish_from_other_restaurant(
    async_client_integration, admin_client, user_a
):
    """Defensa de tenant: el owner del resto A no puede setear el cover de un
    dish del resto B aunque adivine el dish_id."""
    resto_a, _dish_a, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto_a["id"], user_a.user_id)

    resto_b, dish_b, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    # Importante: NO marcamos a user_a como owner de resto_b. Solo es owner de A.

    not_found = await async_client_integration.put(
        f"/api/restaurants/{resto_a['slug']}/dishes/{dish_b}/cover",
        json={"url": "/uploads/cross.jpg"},
        cookies=user_a.cookies,
    )
    assert not_found.status_code == 404


@pytest.mark.asyncio
async def test_admin_bypasses_owner_check_on_dish_cover(
    async_client_integration, admin_client, user_a
):
    resto, dish_id, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    # Sin claim — el admin igual debe poder.
    ok = await async_client_integration.put(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/cover",
        json={"url": "/uploads/admin-set.jpg"},
        cookies=admin_client,
    )
    assert ok.status_code == 200
    assert await _read_dish_cover(dish_id) == "/uploads/admin-set.jpg"


# ── Clear cover ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_clear_dish_cover(
    async_client_integration, admin_client, user_a
):
    resto, dish_id, _ = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    await async_client_integration.put(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/cover",
        json={"url": "/uploads/temp.jpg"},
        cookies=user_a.cookies,
    )
    assert await _read_dish_cover(dish_id) == "/uploads/temp.jpg"

    deleted = await async_client_integration.delete(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/cover",
        cookies=user_a.cookies,
    )
    assert deleted.status_code == 204
    assert await _read_dish_cover(dish_id) is None


# ── Photo candidates ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_photo_candidates_lists_review_images_sorted(
    async_client_integration, admin_client, user_a
):
    """Las fotos UGC se devuelven ordenadas por rating de la review desc.
    Sirve para que el picker del owner muestre primero las "mejores"."""
    resto, dish_id, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    # La review base la creó user_a con score=4.5 (no se puede cambiar acá).
    # Le agregamos dos imágenes a esa review con distintos display_order.
    await _insert_review_image(review_id, "/uploads/r1-img1.jpg", display_order=0)
    await _insert_review_image(review_id, "/uploads/r1-img2.jpg", display_order=1)

    r = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/photo-candidates",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2
    # Misma review, mismo rating — desempate por display_order asc.
    assert items[0]["url"] == "/uploads/r1-img1.jpg"
    assert items[1]["url"] == "/uploads/r1-img2.jpg"
    assert all(it["review_rating"] == 4.5 for it in items)


@pytest.mark.asyncio
async def test_photo_candidates_anonymizes_anonymous_reviews(
    async_client_integration, admin_client, user_a
):
    resto, dish_id, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)

    # Marcar la review como anónima en DB (no hay path API público para esto).
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE dish_reviews SET is_anonymous = true WHERE id = :rid"),
            {"rid": review_id},
        )
    await _insert_review_image(review_id, "/uploads/anon.jpg")

    r = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/photo-candidates",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["is_anonymous"] is True
    assert items[0]["user_display_name"] is None


@pytest.mark.asyncio
async def test_photo_candidates_requires_owner(
    async_client_integration, admin_client, user_a, user_b
):
    resto, dish_id, review_id = await _seed_resto_with_review(
        async_client_integration, admin_client, user_a
    )
    await _make_owner(resto["id"], user_a.user_id)
    await _insert_review_image(review_id, "/uploads/x.jpg")

    forbidden = await async_client_integration.get(
        f"/api/restaurants/{resto['slug']}/dishes/{dish_id}/photo-candidates",
        cookies=user_b.cookies,
    )
    assert forbidden.status_code == 403
