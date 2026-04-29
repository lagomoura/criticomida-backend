"""Integration tests for POST /api/restaurants/{source_id}/merge (Fase 2.2 admin)."""

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


async def _admin_user_id() -> str:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT id FROM users WHERE email = :e"),
            {"e": "admin@criticomida.com"},
        )
        return str(result.scalar_one())


async def _seed_restaurant(*, name: str, place_id: str) -> str:
    rid = str(uuid.uuid4())
    slug = f"pytest-merge-{uuid.uuid4().hex[:8]}"
    async with engine.begin() as conn:
        admin = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        user_id = admin.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO restaurants "
                "(id, slug, name, location_name, latitude, longitude, "
                " google_place_id, computed_rating, review_count, created_by, "
                " created_at, updated_at) "
                "VALUES (:id, :slug, :name, '', -34.6, -58.4, :pid, 0, 0, "
                "        :user, now(), now())"
            ),
            {
                "id": rid,
                "slug": slug,
                "name": name,
                "pid": place_id,
                "user": user_id,
            },
        )
    return rid


async def _seed_dish(restaurant_id: str, name: str) -> str:
    did = str(uuid.uuid4())
    async with engine.begin() as conn:
        creator = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        user_id = creator.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO dishes "
                "(id, restaurant_id, name, computed_rating, review_count, "
                " created_by, created_at) "
                "VALUES (:id, :rid, :name, 0, 0, :user, now())"
            ),
            {"id": did, "rid": restaurant_id, "name": name, "user": user_id},
        )
    return did


async def _seed_review(dish_id: str, rating: float = 4.0) -> str:
    rid = str(uuid.uuid4())
    async with engine.begin() as conn:
        user = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        user_id = user.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO dish_reviews "
                "(id, dish_id, user_id, date_tasted, note, rating, "
                " is_anonymous, created_at, updated_at) "
                "VALUES (:id, :did, :uid, current_date, 'pytest review', "
                "        :rating, false, now(), now())"
            ),
            {"id": rid, "did": dish_id, "uid": user_id, "rating": rating},
        )
    return rid


@pytest.fixture
async def cleanup_merge_data():
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM restaurant_slug_redirects WHERE old_slug LIKE 'pytest-merge-%'")
        )
        await conn.execute(
            text("DELETE FROM restaurants WHERE slug LIKE 'pytest-merge-%' "
                 "OR google_place_id LIKE 'pytest_merge_%'")
        )


@pytest.mark.asyncio
async def test_merge_golden_path(
    async_client_integration, admin_client, cleanup_merge_data
):
    """Source's dishes move to target, source disappears, redirect works."""
    source_id = await _seed_restaurant(
        name="Pizzería Origen", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    target_id = await _seed_restaurant(
        name="Pizzería Destino", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    dish_id = await _seed_dish(source_id, "Calzone Origen")
    await _seed_review(dish_id)

    async with engine.connect() as conn:
        source_slug = (
            await conn.execute(
                text("SELECT slug FROM restaurants WHERE id = :id"),
                {"id": source_id},
            )
        ).scalar_one()

    r = await async_client_integration.post(
        f"/api/restaurants/{source_id}/merge",
        json={"target_id": target_id},
        cookies=admin_client,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dishes_moved"] == 1
    assert body["source_slug"] == source_slug

    async with engine.connect() as conn:
        # Source restaurant is gone.
        gone = (
            await conn.execute(
                text("SELECT 1 FROM restaurants WHERE id = :id"),
                {"id": source_id},
            )
        ).scalar_one_or_none()
        assert gone is None

        # Dish moved to target.
        dish_owner = (
            await conn.execute(
                text("SELECT restaurant_id FROM dishes WHERE id = :id"),
                {"id": dish_id},
            )
        ).scalar_one()
        assert str(dish_owner) == target_id

        # Redirect was inserted.
        redirect_target = (
            await conn.execute(
                text(
                    "SELECT restaurant_id FROM restaurant_slug_redirects "
                    "WHERE old_slug = :slug"
                ),
                {"slug": source_slug},
            )
        ).scalar_one()
        assert str(redirect_target) == target_id

    # GET on the old slug now returns target's data.
    r2 = await async_client_integration.get(f"/api/restaurants/{source_slug}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == target_id


@pytest.mark.asyncio
async def test_merge_dish_name_conflict_remaps_reviews(
    async_client_integration, admin_client, cleanup_merge_data
):
    """When source and target both have a dish with the same normalized name,
    reviews on the source's dish are moved to the target's dish, then the
    source dish is dropped."""
    source_id = await _seed_restaurant(
        name="A", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    target_id = await _seed_restaurant(
        name="B", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    source_dish = await _seed_dish(source_id, "Milanesa")
    target_dish = await _seed_dish(target_id, "milanesa")  # same normalized name
    source_review = await _seed_review(source_dish)

    r = await async_client_integration.post(
        f"/api/restaurants/{source_id}/merge",
        json={"target_id": target_id},
        cookies=admin_client,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reviews_remapped"] == 1
    assert body["dishes_merged_into_target"] == 1
    assert body["dishes_moved"] == 0

    async with engine.connect() as conn:
        # Source dish was deleted.
        source_dish_gone = (
            await conn.execute(
                text("SELECT 1 FROM dishes WHERE id = :id"),
                {"id": source_dish},
            )
        ).scalar_one_or_none()
        assert source_dish_gone is None

        # Review now belongs to target's dish.
        review_dish = (
            await conn.execute(
                text("SELECT dish_id FROM dish_reviews WHERE id = :id"),
                {"id": source_review},
            )
        ).scalar_one()
        assert str(review_dish) == target_dish


@pytest.mark.asyncio
async def test_merge_source_equals_target_returns_400(
    async_client_integration, admin_client, cleanup_merge_data
):
    rid = await _seed_restaurant(
        name="Same", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    r = await async_client_integration.post(
        f"/api/restaurants/{rid}/merge",
        json={"target_id": rid},
        cookies=admin_client,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_merge_target_not_found_returns_404(
    async_client_integration, admin_client, cleanup_merge_data
):
    source_id = await _seed_restaurant(
        name="Lonely", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    fake_target = str(uuid.uuid4())
    r = await async_client_integration.post(
        f"/api/restaurants/{source_id}/merge",
        json={"target_id": fake_target},
        cookies=admin_client,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_merge_requires_admin(
    async_client_integration, user_a, cleanup_merge_data
):
    """Logged-in non-admin gets 403."""
    source_id = await _seed_restaurant(
        name="X", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    target_id = await _seed_restaurant(
        name="Y", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    r = await async_client_integration.post(
        f"/api/restaurants/{source_id}/merge",
        json={"target_id": target_id},
        cookies=user_a.cookies,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_merge_unauthenticated_returns_401(
    async_client_integration, cleanup_merge_data
):
    source_id = await _seed_restaurant(
        name="X", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    target_id = await _seed_restaurant(
        name="Y", place_id=f"pytest_merge_{uuid.uuid4().hex[:8]}"
    )
    r = await async_client_integration.post(
        f"/api/restaurants/{source_id}/merge",
        json={"target_id": target_id},
    )
    assert r.status_code == 401
