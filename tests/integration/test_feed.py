"""Integration tests for feed + review detail."""

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
async def test_feed_for_you_returns_items(async_client_integration, user_a):
    # Use the author's own feed via /api/users/{id}/reviews to avoid the
    # for_you heuristic pushing a fresh zero-engagement review past the first
    # page. We still exercise the feed endpoint, just scoped to the author.
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.get(
        f"/api/users/{user_a.user_id}/reviews?limit=50",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(it["id"] == review_id for it in items)


@pytest.mark.asyncio
async def test_feed_following_empty_without_follows(
    async_client_integration, user_a
):
    # fresh user → no follows → empty following feed
    r = await async_client_integration.get(
        "/api/feed?type=following&limit=10", cookies=user_a.cookies
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_feed_following_shows_followed_users_posts(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )
    r = await async_client_integration.get(
        "/api/feed?type=following&limit=10", cookies=user_b.cookies
    )
    assert r.status_code == 200
    assert any(it["id"] == review_id for it in r.json()["items"])


@pytest.mark.asyncio
async def test_review_detail_includes_extras(
    async_client_integration, user_a
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.get(
        f"/api/reviews/{review_id}", cookies=user_a.cookies
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == review_id
    # Feed list skips extras; detail includes them.
    assert "extras" in body


@pytest.mark.asyncio
async def test_review_detail_404_for_missing(async_client_integration):
    r = await async_client_integration.get(f"/api/reviews/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_feed_anonymous_viewer_state_all_false(
    async_client_integration, user_a
):
    await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.get("/api/feed?type=for_you&limit=1")
    if r.json()["items"]:
        first = r.json()["items"][0]
        vs = first["viewer_state"]
        assert vs["liked"] is False
        assert vs["saved"] is False
        assert vs["following_author"] is False


# --- Fase 4: sort=top en /api/feed?type=following -----------------------------


@pytest.mark.asyncio
async def test_feed_following_sort_recent_default(
    async_client_integration, user_a, user_b
):
    """Sort default = recent: las reviews del followed aparecen ordenadas por
    created_at DESC (la más nueva primero)."""
    # B sigue a A.
    await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )
    # A crea 2 reviews. La segunda es más nueva que la primera.
    first = await create_review(async_client_integration, user_a.cookies, score=3.0)
    second = await create_review(async_client_integration, user_a.cookies, score=3.5)

    r = await async_client_integration.get(
        "/api/feed?type=following&limit=10", cookies=user_b.cookies
    )
    assert r.status_code == 200
    ids = [it["id"] for it in r.json()["items"]]
    # second creado después que first → debe ir antes en orden cronológico.
    assert ids.index(second) < ids.index(first)


@pytest.mark.asyncio
async def test_feed_following_sort_top_prioritizes_high_rating(
    async_client_integration, user_a, user_b
):
    """Sort=top usa rank_by_priority: una review con rating ≥4 + pilares
    completos pesa más que una con rating bajo, aun siendo más antigua."""
    await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )
    # Primera (más vieja): rating bajo, sin pilares — priority floor.
    low = await create_review(
        async_client_integration, user_a.cookies, score=2.5
    )
    # Segunda (más nueva): rating alto + 3 pilares completos. La fórmula de
    # priority en _build_feed_items le suma +2 por rating ≥4 + boost por
    # recencia. Debe quedar arriba en sort=top.
    high = await create_review(
        async_client_integration,
        user_a.cookies,
        score=5.0,
        presentation=3,
        value_prop=3,
        execution=3,
    )

    r = await async_client_integration.get(
        "/api/feed?type=following&sort=top&limit=10", cookies=user_b.cookies
    )
    assert r.status_code == 200
    ids = [it["id"] for it in r.json()["items"]]
    assert ids.index(high) < ids.index(low)


@pytest.mark.asyncio
async def test_feed_sort_invalid_value_400(async_client_integration, user_a):
    """sort solo acepta 'recent' | 'top'. Cualquier otro valor → 422 (Pydantic
    Literal mismatch)."""
    r = await async_client_integration.get(
        "/api/feed?type=following&sort=garbage", cookies=user_a.cookies
    )
    assert r.status_code == 422


# --- Fase 4: verified_by_expert en FeedItem -----------------------------------


@pytest.mark.asyncio
async def test_review_verified_by_expert_true_when_3_pillars(
    async_client_integration, user_a
):
    """Una review con los 3 pilares completos debe tener verified_by_expert=True
    tanto en el listado como en el detail."""
    review_id = await create_review(
        async_client_integration,
        user_a.cookies,
        presentation=2,
        value_prop=2,
        execution=2,
    )
    detail = await async_client_integration.get(
        f"/api/reviews/{review_id}", cookies=user_a.cookies
    )
    assert detail.json()["verified_by_expert"] is True


@pytest.mark.asyncio
async def test_review_verified_by_expert_false_when_partial(
    async_client_integration, user_a
):
    """Si falta cualquiera de los 3 pilares, verified_by_expert=False."""
    review_id = await create_review(
        async_client_integration,
        user_a.cookies,
        presentation=3,
        execution=3,
        # value_prop ausente → no califica como verificada.
    )
    detail = await async_client_integration.get(
        f"/api/reviews/{review_id}", cookies=user_a.cookies
    )
    assert detail.json()["verified_by_expert"] is False


@pytest.mark.asyncio
async def test_review_verified_by_expert_false_when_no_pillars(
    async_client_integration, user_a
):
    """Review sin pilares (la mayoría de reviews históricas) no es expert."""
    review_id = await create_review(async_client_integration, user_a.cookies)
    detail = await async_client_integration.get(
        f"/api/reviews/{review_id}", cookies=user_a.cookies
    )
    assert detail.json()["verified_by_expert"] is False
