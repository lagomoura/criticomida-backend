"""Integration tests for POST /api/dishes/{source_id}/merge (Capa 4 admin)."""

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


# --- helpers ---------------------------------------------------------------


async def _seeded_user_id() -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(text("SELECT id FROM users LIMIT 1"))
            ).scalar_one()
        )


async def _seed_restaurant(*, name: str, place_id: str) -> str:
    rid = str(uuid.uuid4())
    slug = f"pytest-dishmerge-{uuid.uuid4().hex[:8]}"
    user_id = await _seeded_user_id()
    async with engine.begin() as conn:
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


async def _seed_dish(
    restaurant_id: str, name: str, *, cover_url: str | None = None
) -> str:
    did = str(uuid.uuid4())
    user_id = await _seeded_user_id()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dishes "
                "(id, restaurant_id, name, cover_image_url, "
                " computed_rating, review_count, created_by, created_at) "
                "VALUES (:id, :rid, :name, :cover, 0, 0, :user, now())"
            ),
            {
                "id": did,
                "rid": restaurant_id,
                "name": name,
                "cover": cover_url,
                "user": user_id,
            },
        )
    return did


async def _seed_review(dish_id: str, *, rating: float = 4.0) -> str:
    rid = str(uuid.uuid4())
    user_id = await _seeded_user_id()
    async with engine.begin() as conn:
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
async def cleanup_dishmerge_data():
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM restaurants "
                "WHERE slug LIKE 'pytest-dishmerge-%' "
                "   OR google_place_id LIKE 'pytest_dishmerge_%'"
            )
        )


# --- tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_golden_path(
    async_client_integration, admin_client, cleanup_dishmerge_data
):
    """Source's reviews move to target, source row is gone, target rating
    recomputed across the new combined review set."""
    rest_id = await _seed_restaurant(
        name="Golden Path", place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}"
    )
    source = await _seed_dish(rest_id, "Muzzarela")
    target = await _seed_dish(rest_id, "Muzzarella")
    await _seed_review(source, rating=5.0)
    await _seed_review(source, rating=3.0)
    await _seed_review(target, rating=4.0)

    r = await async_client_integration.post(
        f"/api/dishes/{source}/merge",
        json={"target_id": target},
        cookies=admin_client,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reviews_moved"] == 2
    assert body["source_id"] == source
    assert body["target_id"] == target
    assert body["cover_inherited"] is False  # neither had a cover

    async with engine.connect() as conn:
        # Source dish row is gone.
        gone = (
            await conn.execute(
                text("SELECT 1 FROM dishes WHERE id = :id"),
                {"id": source},
            )
        ).scalar_one_or_none()
        assert gone is None

        # Target now has all three reviews.
        review_count = (
            await conn.execute(
                text("SELECT count(*) FROM dish_reviews WHERE dish_id = :id"),
                {"id": target},
            )
        ).scalar_one()
        assert review_count == 3

        # Target rating recomputed: avg(5, 3, 4) = 4.0.
        target_row = (
            await conn.execute(
                text(
                    "SELECT computed_rating, review_count "
                    "FROM dishes WHERE id = :id"
                ),
                {"id": target},
            )
        ).one()
        assert float(target_row.computed_rating) == 4.0
        assert target_row.review_count == 3


@pytest.mark.asyncio
async def test_merge_inherits_cover_when_target_has_none(
    async_client_integration, admin_client, cleanup_dishmerge_data
):
    """If the target lacks a cover image but the source has one, the merge
    moves the URL to the target so we don't lose the only photo."""
    rest_id = await _seed_restaurant(
        name="Cover Inherit",
        place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}",
    )
    source = await _seed_dish(
        rest_id, "WithCover", cover_url="https://example.com/photo.jpg"
    )
    target = await _seed_dish(rest_id, "WithoutCover")

    r = await async_client_integration.post(
        f"/api/dishes/{source}/merge",
        json={"target_id": target},
        cookies=admin_client,
    )
    assert r.status_code == 200, r.text
    assert r.json()["cover_inherited"] is True

    async with engine.connect() as conn:
        cover = (
            await conn.execute(
                text("SELECT cover_image_url FROM dishes WHERE id = :id"),
                {"id": target},
            )
        ).scalar_one()
        assert cover == "https://example.com/photo.jpg"


@pytest.mark.asyncio
async def test_merge_keeps_target_cover_when_both_have_one(
    async_client_integration, admin_client, cleanup_dishmerge_data
):
    """Target's existing cover wins — admin chose target as canonical, so
    target's photo is the canonical one too."""
    rest_id = await _seed_restaurant(
        name="Both Cover", place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}"
    )
    source = await _seed_dish(
        rest_id, "S", cover_url="https://example.com/source.jpg"
    )
    target = await _seed_dish(
        rest_id, "T", cover_url="https://example.com/target.jpg"
    )

    r = await async_client_integration.post(
        f"/api/dishes/{source}/merge",
        json={"target_id": target},
        cookies=admin_client,
    )
    assert r.status_code == 200, r.text
    assert r.json()["cover_inherited"] is False

    async with engine.connect() as conn:
        cover = (
            await conn.execute(
                text("SELECT cover_image_url FROM dishes WHERE id = :id"),
                {"id": target},
            )
        ).scalar_one()
        assert cover == "https://example.com/target.jpg"


@pytest.mark.asyncio
async def test_merge_rejects_same_id(
    async_client_integration, admin_client, cleanup_dishmerge_data
):
    rest_id = await _seed_restaurant(
        name="Same Id", place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}"
    )
    dish_id = await _seed_dish(rest_id, "Solo")

    r = await async_client_integration.post(
        f"/api/dishes/{dish_id}/merge",
        json={"target_id": dish_id},
        cookies=admin_client,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_merge_rejects_cross_restaurant(
    async_client_integration, admin_client, cleanup_dishmerge_data
):
    """Merging dishes that live in different restaurants is not allowed —
    that's a different problem (use restaurant merge instead)."""
    rest_a = await _seed_restaurant(
        name="A", place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}"
    )
    rest_b = await _seed_restaurant(
        name="B", place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}"
    )
    dish_a = await _seed_dish(rest_a, "Plato A")
    dish_b = await _seed_dish(rest_b, "Plato B")

    r = await async_client_integration.post(
        f"/api/dishes/{dish_a}/merge",
        json={"target_id": dish_b},
        cookies=admin_client,
    )
    assert r.status_code == 400
    assert "same restaurant" in r.json()["detail"]


@pytest.mark.asyncio
async def test_merge_404_when_target_missing(
    async_client_integration, admin_client, cleanup_dishmerge_data
):
    rest_id = await _seed_restaurant(
        name="Missing Target",
        place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}",
    )
    source = await _seed_dish(rest_id, "Real")

    r = await async_client_integration.post(
        f"/api/dishes/{source}/merge",
        json={"target_id": str(uuid.uuid4())},
        cookies=admin_client,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_merge_requires_admin(
    async_client_integration, user_a, cleanup_dishmerge_data
):
    """Non-admin gets 403 even with a valid payload."""
    rest_id = await _seed_restaurant(
        name="Auth Test", place_id=f"pytest_dishmerge_{uuid.uuid4().hex[:8]}"
    )
    source = await _seed_dish(rest_id, "S")
    target = await _seed_dish(rest_id, "T")

    r = await async_client_integration.post(
        f"/api/dishes/{source}/merge",
        json={"target_id": target},
        cookies=user_a.cookies,
    )
    assert r.status_code == 403
