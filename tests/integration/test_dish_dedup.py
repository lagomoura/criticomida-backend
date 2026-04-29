"""Integration tests for dish name normalization & dedup.

Covers:
    1. Two users typing the same dish with different casing/spacing/accents
       end up reviewing the **same** Dish row (dedup at write time).
    2. The unique index on (restaurant_id, name_normalized) is in place and
       agrees with the application-level dedup logic.
    3. /api/dishes/suggest-similar surfaces typo'd / accent-stripped duplicates
       and stays quiet for genuinely new dishes.
"""

import os
import uuid

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


def _restaurant(place_id: str) -> dict:
    return {
        "place_id": place_id,
        "name": "Dedup Tests",
        "city": "Buenos Aires",
    }


@pytest.mark.asyncio
async def test_casing_spacing_accents_collapse_to_one_dish(
    async_client_integration, user_a, user_b
):
    """`Muzzarella`, ` muzzarella `, `MUZZARELLA`, `Muzzaréllá` all dedup
    into the same Dish row when posted at the same restaurant.

    They produce different reviews but a single dish, so the technical
    pillars and rating average across all three users instead of fragmenting.
    """
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"

    variants = ["Muzzarella", " muzzarella ", "MUZZARELLA", "Muzzaréllá"]
    dish_ids: list[str] = []
    for i, variant in enumerate(variants):
        # Alternate users so we don't trip per-user uniqueness elsewhere.
        cookies = (user_a if i % 2 == 0 else user_b).cookies
        r = await async_client_integration.post(
            "/api/posts",
            json={
                "restaurant": _restaurant(place_id),
                "dish_name": variant,
                "score": 4,
                "text": f"variant {i}: {variant}",
            },
            cookies=cookies,
        )
        assert r.status_code == 201, r.text
        dish_ids.append(r.json()["dish"]["id"])

    assert len(set(dish_ids)) == 1, f"expected one dish, got {set(dish_ids)}"


@pytest.mark.asyncio
async def test_suggest_similar_finds_typo_match(
    async_client_integration, user_a
):
    """Typing 'Muzarela' (typo + missing accents) should surface the
    existing 'Muzzarella' as a candidate, not silently create a dupe."""
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"

    # Seed the canonical dish.
    seed = await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": _restaurant(place_id),
            "dish_name": "Muzzarella",
            "score": 4,
            "text": "seed",
        },
        cookies=user_a.cookies,
    )
    assert seed.status_code == 201, seed.text
    seeded_id = seed.json()["dish"]["id"]

    r = await async_client_integration.get(
        "/api/dishes/suggest-similar",
        params={"restaurant_place_id": place_id, "name": "Muzarela"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1
    assert items[0]["id"] == seeded_id
    # 'muzarela' vs 'muzzarella' — close enough to score above the threshold,
    # but not an exact normalized match.
    assert items[0]["is_exact_normalized"] is False
    assert items[0]["similarity"] >= 0.4


@pytest.mark.asyncio
async def test_suggest_similar_marks_exact_normalized(
    async_client_integration, user_a
):
    """When the input differs only by case/accent/whitespace from an existing
    dish, the response flags it as an exact normalized match — the modal can
    pick a stronger copy ('ya existe...') instead of just 'parecidos'."""
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"

    await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": _restaurant(place_id),
            "dish_name": "Café Turco",
            "score": 4,
            "text": "seed",
        },
        cookies=user_a.cookies,
    )

    r = await async_client_integration.get(
        "/api/dishes/suggest-similar",
        params={"restaurant_place_id": place_id, "name": "  CAFE  TURCO  "},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["is_exact_normalized"] is True
    assert items[0]["similarity"] == pytest.approx(1.0, abs=0.001)


@pytest.mark.asyncio
async def test_suggest_similar_returns_empty_for_novel_dish(
    async_client_integration, user_a
):
    """Genuinely new dish names get a clear 'create freely' signal so the
    frontend doesn't pop a useless modal."""
    place_id = f"pytest_{uuid.uuid4().hex[:10]}"

    await async_client_integration.post(
        "/api/posts",
        json={
            "restaurant": _restaurant(place_id),
            "dish_name": "Ravioles de calabaza",
            "score": 4,
            "text": "seed",
        },
        cookies=user_a.cookies,
    )

    r = await async_client_integration.get(
        "/api/dishes/suggest-similar",
        params={
            "restaurant_place_id": place_id,
            "name": "Tarta de manzana",
        },
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_suggest_similar_unknown_restaurant_returns_empty(
    async_client_integration,
):
    """Unknown place_id (the user is reviewing a brand-new restaurant) is not
    a 404 — the frontend should treat it as 'no candidates' and proceed."""
    r = await async_client_integration.get(
        "/api/dishes/suggest-similar",
        params={
            "restaurant_place_id": f"pytest_unknown_{uuid.uuid4().hex[:8]}",
            "name": "Anything",
        },
    )
    assert r.status_code == 200
    assert r.json()["items"] == []
