"""Integration tests for editing the dish name from PUT /api/dish-reviews/{id}.

The dish is shared across users, so a rename never mutates the existing Dish
row globally — instead the review is re-linked (find-or-create) to a Dish in
the same restaurant whose `name_normalized` matches the new name.
"""

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


def _restaurant(place_id: str) -> dict:
    return {
        "place_id": place_id,
        "name": "Rename Tests",
        "city": "Buenos Aires",
    }


async def _seed_review(client, cookies, *, place_id: str, dish_name: str) -> dict:
    r = await client.post(
        "/api/posts",
        json={
            "restaurant": _restaurant(place_id),
            "dish_name": dish_name,
            "score": 4.0,
            "text": "Reseña base para los tests de rename.",
        },
        cookies=cookies,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_rename_to_normalized_equivalent_is_noop(
    async_client_integration, user_a
):
    """Cambiar la grafía sin alterar el normalized (mayúsculas/acentos/espacios)
    no debe re-linkear: el Dish compartido se queda como está."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    feed = await _seed_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        dish_name="Muzzarella",
    )
    review_id = feed["id"]
    original_dish_id = feed["dish"]["id"]

    r = await async_client_integration.put(
        f"/api/dish-reviews/{review_id}",
        json={"dish_name": "  MUZZARÉLLA  "},
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json()["dish_id"] == original_dish_id


@pytest.mark.asyncio
async def test_rename_creates_new_dish_when_no_match(
    async_client_integration, user_a, user_b
):
    """Si el normalized cambia y no hay dish existente, se crea uno nuevo y la
    review queda relinkeada. El dish viejo sigue existiendo si otro user tiene
    una review en él (no se borra global)."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"

    # user_a y user_b reseñan el mismo plato → un solo Dish compartido.
    feed_a = await _seed_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        dish_name="Pizza",
    )
    feed_b = await _seed_review(
        async_client_integration,
        user_b.cookies,
        place_id=place_id,
        dish_name="Pizza",
    )
    assert feed_a["dish"]["id"] == feed_b["dish"]["id"]
    original_dish_id = feed_a["dish"]["id"]

    # user_a renombra. El dish original sigue intacto (user_b lo conserva).
    r = await async_client_integration.put(
        f"/api/dish-reviews/{feed_a['id']}",
        json={"dish_name": "Pizza margherita"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    new_dish_id = r.json()["dish_id"]
    assert new_dish_id != original_dish_id

    async with engine.begin() as conn:
        old = (
            await conn.execute(
                text("SELECT name, restaurant_id FROM dishes WHERE id = :id"),
                {"id": original_dish_id},
            )
        ).first()
        new = (
            await conn.execute(
                text("SELECT name, restaurant_id FROM dishes WHERE id = :id"),
                {"id": new_dish_id},
            )
        ).first()
        assert old is not None and old[0] == "Pizza"
        assert new is not None and new[0] == "Pizza margherita"
        # El nuevo dish vive en el mismo restaurante.
        assert str(new[1]) == str(old[1])


@pytest.mark.asyncio
async def test_rename_links_to_existing_dish_when_normalized_matches(
    async_client_integration, user_a
):
    """Si el restaurant ya tiene un Dish con el mismo normalized, la review
    se re-linkea a ese Dish en vez de crear un duplicado."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"

    feed_other = await _seed_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        dish_name="Milanesa napolitana",
    )
    target_dish_id = feed_other["dish"]["id"]

    feed = await _seed_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        dish_name="Milanesa común",
    )
    assert feed["dish"]["id"] != target_dish_id

    r = await async_client_integration.put(
        f"/api/dish-reviews/{feed['id']}",
        json={"dish_name": "  MILANESA  napolitana "},
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text
    assert r.json()["dish_id"] == target_dish_id


@pytest.mark.asyncio
async def test_rename_recomputes_old_dish_review_count(
    async_client_integration, user_a
):
    """Cuando una review se va de un Dish que sólo tenía esa review, el
    review_count del Dish viejo cae a 0 (el Dish queda huérfano pero válido,
    a la espera de que un admin lo limpie con el merge tool)."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"

    feed = await _seed_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        dish_name="Plato exclusivo",
    )
    old_dish_id = feed["dish"]["id"]

    r = await async_client_integration.put(
        f"/api/dish-reviews/{feed['id']}",
        json={"dish_name": "Plato renombrado"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 200, r.text

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT review_count FROM dishes WHERE id = :id"),
                {"id": old_dish_id},
            )
        ).first()
        assert row is not None
        assert row[0] == 0


@pytest.mark.asyncio
async def test_rename_rejects_blank_name(async_client_integration, user_a):
    """`dish_name` vacío o sólo espacios → 422, no se silenciosa-mente ignora."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"
    feed = await _seed_review(
        async_client_integration,
        user_a.cookies,
        place_id=place_id,
        dish_name="Algo",
    )

    r = await async_client_integration.put(
        f"/api/dish-reviews/{feed['id']}",
        json={"dish_name": "   "},
        cookies=user_a.cookies,
    )
    assert r.status_code == 422

    r2 = await async_client_integration.put(
        f"/api/dish-reviews/{feed['id']}",
        json={"dish_name": ""},
        cookies=user_a.cookies,
    )
    assert r2.status_code == 422
