"""Integration tests for the dish wishlist ('Quiero probarlo')."""

import os

import pytest

from tests.integration.conftest import create_review

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


async def _dish_id_from_review(client, cookies, review_id) -> str:
    """Read the review and return its dish_id (the wishlist key)."""
    r = await client.get(f"/api/reviews/{review_id}", cookies=cookies)
    assert r.status_code == 200
    return r.json()["dish"]["id"]


@pytest.mark.asyncio
async def test_add_remove_round_trip(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    dish_id = await _dish_id_from_review(
        async_client_integration, user_b.cookies, review_id
    )

    add = await async_client_integration.post(
        f"/api/dishes/{dish_id}/want-to-try", cookies=user_b.cookies
    )
    assert add.status_code == 200
    assert add.json()["want_to_try"] is True

    listing = await async_client_integration.get(
        "/api/users/me/want-to-try", cookies=user_b.cookies
    )
    assert listing.status_code == 200
    assert any(it["dish_id"] == dish_id for it in listing.json()["items"])

    rem = await async_client_integration.delete(
        f"/api/dishes/{dish_id}/want-to-try", cookies=user_b.cookies
    )
    assert rem.status_code == 200
    assert rem.json()["want_to_try"] is False

    listing = await async_client_integration.get(
        "/api/users/me/want-to-try", cookies=user_b.cookies
    )
    assert all(it["dish_id"] != dish_id for it in listing.json()["items"])


@pytest.mark.asyncio
async def test_add_is_idempotent(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    dish_id = await _dish_id_from_review(
        async_client_integration, user_b.cookies, review_id
    )
    first = await async_client_integration.post(
        f"/api/dishes/{dish_id}/want-to-try", cookies=user_b.cookies
    )
    second = await async_client_integration.post(
        f"/api/dishes/{dish_id}/want-to-try", cookies=user_b.cookies
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["want_to_try"] is True
    assert second.json()["want_to_try"] is True


@pytest.mark.asyncio
async def test_unknown_dish_returns_404(async_client_integration, user_a):
    fake_uuid = "00000000-0000-0000-0000-000000000099"
    r = await async_client_integration.post(
        f"/api/dishes/{fake_uuid}/want-to-try", cookies=user_a.cookies
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_listing_requires_auth(async_client_integration):
    r = await async_client_integration.get("/api/users/me/want-to-try")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_viewer_state_in_feed(async_client_integration, user_a, user_b):
    """El viewer state del feed expone want_to_try después de agregar."""
    review_id = await create_review(async_client_integration, user_a.cookies)
    dish_id = await _dish_id_from_review(
        async_client_integration, user_b.cookies, review_id
    )

    await async_client_integration.post(
        f"/api/dishes/{dish_id}/want-to-try", cookies=user_b.cookies
    )

    feed = await async_client_integration.get(
        "/api/feed?type=for_you", cookies=user_b.cookies
    )
    assert feed.status_code == 200
    target = next(
        (it for it in feed.json()["items"] if it["id"] == review_id), None
    )
    assert target is not None
    assert target["viewer_state"]["want_to_try"] is True
