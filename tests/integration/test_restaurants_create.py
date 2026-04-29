"""Integration tests for POST /api/restaurants — google_place_id dedup."""

import asyncio
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


def _payload(place_id: str | None, *, name: str | None = None) -> dict:
    return {
        "slug": "",
        "name": name or f"Resto {uuid.uuid4().hex[:6]}",
        "location_name": "Av. Test 1234, CABA",
        "google_place_id": place_id,
    }


@pytest.mark.asyncio
async def test_create_dedupes_by_place_id(async_client_integration, admin_client):
    """Second POST with the same google_place_id returns the existing row."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"

    first = await async_client_integration.post(
        "/api/restaurants",
        json=_payload(place_id, name="Original Name"),
        cookies=admin_client,
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["existed"] is False
    assert first_body["google_place_id"] == place_id

    # Same place_id, different name — should return the original, not create new
    second = await async_client_integration.post(
        "/api/restaurants",
        json=_payload(place_id, name="Different Name Same Place"),
        cookies=admin_client,
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["existed"] is True
    assert second_body["id"] == first_body["id"]
    assert second_body["slug"] == first_body["slug"]
    assert second_body["name"] == "Original Name"  # preserved


@pytest.mark.asyncio
async def test_create_without_place_id_always_creates_new(
    async_client_integration, admin_client
):
    """No place_id → no dedup, each POST is a fresh row."""
    name = f"NoPlaceId {uuid.uuid4().hex[:8]}"

    first = await async_client_integration.post(
        "/api/restaurants",
        json=_payload(None, name=name),
        cookies=admin_client,
    )
    assert first.status_code == 201, first.text
    assert first.json()["existed"] is False

    second = await async_client_integration.post(
        "/api/restaurants",
        json=_payload(None, name=name),  # same name
        cookies=admin_client,
    )
    assert second.status_code == 201, second.text
    assert second.json()["existed"] is False
    assert second.json()["id"] != first.json()["id"]
    # Slug retry path — second row got a uuid suffix.
    assert second.json()["slug"] != first.json()["slug"]

    # Cleanup: NULL place_id rows aren't matched by the session-level cleanup
    # (which keys off pytest_place_* pattern). Delete explicitly here.
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM restaurants WHERE id IN (:a, :b)"),
            {"a": first.json()["id"], "b": second.json()["id"]},
        )


@pytest.mark.asyncio
async def test_create_concurrent_same_place_id_dedupes(
    async_client_integration, admin_client
):
    """Two concurrent POSTs with the same place_id end up with one restaurant."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"

    async def do_post():
        return await async_client_integration.post(
            "/api/restaurants",
            json=_payload(place_id, name=f"Concurrent {uuid.uuid4().hex[:4]}"),
            cookies=admin_client,
        )

    r1, r2 = await asyncio.gather(do_post(), do_post())
    assert r1.status_code in (200, 201), r1.text
    assert r2.status_code in (200, 201), r2.text

    # Exactly one should be 201 (created), exactly one should be 200 (existed).
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 201], f"unexpected status pair: {statuses}"

    # Both responses point to the same restaurant id.
    assert r1.json()["id"] == r2.json()["id"]


@pytest.mark.asyncio
async def test_db_unique_index_blocks_duplicate_place_id():
    """Direct DB INSERT with a duplicate place_id is rejected by the index."""
    place_id = f"pytest_place_{uuid.uuid4().hex[:10]}"

    async with engine.begin() as conn:
        creator = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        user_id = creator.scalar_one()
        await conn.execute(
            text(
                "INSERT INTO restaurants "
                "(id, slug, name, location_name, google_place_id, computed_rating, "
                " review_count, created_by, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :slug, :name, '', :pid, 0, 0, :user, "
                "        now(), now())"
            ),
            {
                "slug": f"pytest-uq-{uuid.uuid4().hex[:8]}",
                "name": "PyTest Uniq A",
                "pid": place_id,
                "user": user_id,
            },
        )

    # Second insert with same place_id must fail.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            creator = await conn.execute(text("SELECT id FROM users LIMIT 1"))
            user_id = creator.scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO restaurants "
                    "(id, slug, name, location_name, google_place_id, "
                    " computed_rating, review_count, created_by, created_at, "
                    " updated_at) "
                    "VALUES (gen_random_uuid(), :slug, :name, '', :pid, 0, 0, "
                    "        :user, now(), now())"
                ),
                {
                    "slug": f"pytest-uq-{uuid.uuid4().hex[:8]}",
                    "name": "PyTest Uniq B",
                    "pid": place_id,
                    "user": user_id,
                },
            )

    # Two NULL place_ids must be allowed (partial index).
    async with engine.begin() as conn:
        creator = await conn.execute(text("SELECT id FROM users LIMIT 1"))
        user_id = creator.scalar_one()
        for _ in range(2):
            await conn.execute(
                text(
                    "INSERT INTO restaurants "
                    "(id, slug, name, location_name, google_place_id, "
                    " computed_rating, review_count, created_by, created_at, "
                    " updated_at) "
                    "VALUES (gen_random_uuid(), :slug, :name, '', NULL, 0, 0, "
                    "        :user, now(), now())"
                ),
                {
                    "slug": f"pytest-null-{uuid.uuid4().hex[:8]}",
                    "name": f"PyTest Null {uuid.uuid4().hex[:6]}",
                    "user": user_id,
                },
            )

        # Cleanup: place_id pattern catches the non-NULL one; NULL ones must be
        # removed by slug.
        await conn.execute(
            text("DELETE FROM restaurants WHERE slug LIKE 'pytest-null-%'")
        )
