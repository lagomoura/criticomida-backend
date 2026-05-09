"""Integration tests for /api/search.

The search endpoint matches a word-prefix on the canonical field(s) per
entity (Dish.name, Restaurant.name, and both User.handle and
User.display_name), case- and accent-insensitive, and returns results
sorted alphabetically.
"""

import os
import uuid

import pytest

from tests.integration.conftest import create_review

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_search_matches_dish_and_restaurant_by_word_prefix(
    async_client_integration, user_a
):
    token = uuid.uuid4().hex[:8]
    await create_review(
        async_client_integration,
        user_a.cookies,
        restaurant_name=f"Pytest Bistro {token}",
        dish_name=f"Plato {token}",
    )

    r = await async_client_integration.get(f"/api/search?q={token}")
    assert r.status_code == 200
    body = r.json()
    # token sits at a word boundary in both names → both tabs match.
    assert any(d["name"].endswith(token) for d in body["dishes"])
    assert any(rest["name"].endswith(token) for rest in body["restaurants"])


@pytest.mark.asyncio
async def test_search_matches_user_by_handle_prefix(
    async_client_integration, user_a
):
    handle = f"pytest{uuid.uuid4().hex[:6]}"
    patch = await async_client_integration.patch(
        "/api/users/me", json={"handle": handle}, cookies=user_a.cookies
    )
    assert patch.status_code == 200, patch.text

    # Full handle prefix matches.
    r = await async_client_integration.get(f"/api/search?q={handle}")
    assert r.status_code == 200
    assert any(u["handle"] == handle for u in r.json()["users"])

    # Just the first few letters of the handle also matches.
    r2 = await async_client_integration.get(f"/api/search?q={handle[:3]}")
    assert r2.status_code == 200
    assert any(u["handle"] == handle for u in r2.json()["users"])


@pytest.mark.asyncio
async def test_search_matches_user_by_display_name(
    async_client_integration, user_a
):
    """display_name puede divergir del handle (ej. 'Julián Pérez' vs 'julianp');
    buscar por el nombre visible debe encontrar al usuario igualmente."""
    token = uuid.uuid4().hex[:6]
    handle = f"u{token}"
    display = f"Julián {token}"
    patch = await async_client_integration.patch(
        "/api/users/me",
        json={"handle": handle, "display_name": display},
        cookies=user_a.cookies,
    )
    assert patch.status_code == 200, patch.text

    # Buscar por la primera palabra del display_name (acento incluido) hace match
    # aunque el handle no contenga "Julián".
    r = await async_client_integration.get("/api/search?q=Juli")
    assert r.status_code == 200
    assert any(u["handle"] == handle for u in r.json()["users"])


@pytest.mark.asyncio
async def test_search_does_not_match_mid_word(
    async_client_integration, user_a
):
    """A substring that lives inside a word (no whitespace before) is rejected."""
    suffix = uuid.uuid4().hex[:6]
    embedded = f"zzz{suffix}"
    dish_name = f"Cosa{embedded}"  # single word, embedded is mid-word
    await create_review(
        async_client_integration,
        user_a.cookies,
        restaurant_name=f"Resto {suffix}",
        dish_name=dish_name,
    )
    r = await async_client_integration.get(f"/api/search?q={embedded}")
    assert r.status_code == 200
    assert not any(d["name"] == dish_name for d in r.json()["dishes"])


@pytest.mark.asyncio
async def test_search_is_accent_and_case_insensitive(
    async_client_integration, user_a
):
    suffix = uuid.uuid4().hex[:6]
    dish_name = f"Año {suffix}"
    await create_review(
        async_client_integration,
        user_a.cookies,
        restaurant_name=f"Resto {suffix}",
        dish_name=dish_name,
    )
    # ASCII + uppercase still hits the accented dish name.
    r = await async_client_integration.get("/api/search?q=ANO")
    assert r.status_code == 200
    assert any(d["name"] == dish_name for d in r.json()["dishes"])


@pytest.mark.asyncio
async def test_search_dishes_are_alphabetical(
    async_client_integration, user_a
):
    """Dishes sharing the same query prefix come back A→Z."""
    suffix = uuid.uuid4().hex[:6]
    prefix = f"alpha{suffix}"
    # Insert in non-alphabetical order; expect alphabetical response.
    for word in ("Charlie", "Alpha", "Bravo"):
        await create_review(
            async_client_integration,
            user_a.cookies,
            restaurant_name=f"R-{word}-{suffix}",
            dish_name=f"{prefix} {word}",
        )
    r = await async_client_integration.get(f"/api/search?q={prefix}")
    assert r.status_code == 200
    names = [d["name"] for d in r.json()["dishes"] if d["name"].startswith(prefix)]
    assert names == sorted(names)
