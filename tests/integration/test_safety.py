"""Integration tests for the block / mute (safety) endpoints.

Cubre las cuatro superficies que la migración 055 endurece:

- ``/api/users/{id}/block`` y ``/mute`` (router safety).
- ``/api/users/{id}/follow`` rechaza si hay block bidireccional.
- ``/api/notifications`` no recibe acciones de actores bloqueados/muteados.
- ``/api/feed`` excluye reviews de autores bloqueados/muteados.
"""

import os

import pytest

from tests.integration.conftest import create_review

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


# ----- block -----------------------------------------------------------


@pytest.mark.asyncio
async def test_block_is_idempotent(async_client_integration, user_a, user_b):
    first = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )
    second = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["blocked"] is True
    assert second.json()["blocked"] is True


@pytest.mark.asyncio
async def test_self_block_rejected(async_client_integration, user_a):
    r = await async_client_integration.post(
        f"/api/users/{user_a.user_id}/block", cookies=user_a.cookies
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_block_auto_unfollows_both_directions(
    async_client_integration, user_a, user_b
):
    # A sigue a B, B sigue a A
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )

    # A bloquea a B
    r = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )
    assert r.status_code == 200

    # ninguno aparece en el following del otro
    a_following = await async_client_integration.get(
        f"/api/users/{user_a.user_id}/following", cookies=user_a.cookies
    )
    b_following = await async_client_integration.get(
        f"/api/users/{user_b.user_id}/following", cookies=user_b.cookies
    )
    a_followers = await async_client_integration.get(
        f"/api/users/{user_a.user_id}/followers", cookies=user_a.cookies
    )
    a_following_ids = [u["id"] for u in a_following.json()["items"]]
    b_following_ids = [u["id"] for u in b_following.json()["items"]]
    a_followers_ids = [u["id"] for u in a_followers.json()["items"]]
    assert user_b.user_id not in a_following_ids
    assert user_a.user_id not in b_following_ids
    assert user_b.user_id not in a_followers_ids


@pytest.mark.asyncio
async def test_blocked_user_cannot_follow(
    async_client_integration, user_a, user_b
):
    """Tras un block, el bloqueado no puede iniciar follow (devuelve 404
    para no filtrar quién bloqueó a quién)."""
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )

    # B intenta seguir a A
    r = await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )
    assert r.status_code == 404

    # A también está bloqueado para iniciar follow hacia B (la dirección
    # original del block es indistinta)
    r = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_block_excludes_review_from_feed(
    async_client_integration, user_a, user_b
):
    """B publica una review; A la sigue (la ve); A bloquea a B → la
    review desaparece del following feed de A."""
    review_id = await create_review(async_client_integration, user_b.cookies)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )

    before = await async_client_integration.get(
        "/api/feed?type=following&limit=50", cookies=user_a.cookies
    )
    assert any(it["id"] == review_id for it in before.json()["items"])

    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )

    after = await async_client_integration.get(
        "/api/feed?type=following&limit=50", cookies=user_a.cookies
    )
    # Following se vacía (auto-unfollow) y aún en for_you no debería aparecer
    assert all(it["id"] != review_id for it in after.json()["items"])

    after_for_you = await async_client_integration.get(
        "/api/feed?type=for_you&limit=50", cookies=user_a.cookies
    )
    assert all(it["id"] != review_id for it in after_for_you.json()["items"])


@pytest.mark.asyncio
async def test_block_suppresses_like_notification(
    async_client_integration, user_a, user_b
):
    """A bloquea a B. B le da like a una review de A. A no recibe notif."""
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )

    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_b.cookies
    )
    assert r.status_code == 200

    notifs = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_a.cookies
        )
    ).json()["items"]
    assert all(
        n.get("actor", {}).get("id") != user_b.user_id
        and n.get("actor_user_id") != user_b.user_id
        for n in notifs
    )


@pytest.mark.asyncio
async def test_unblock_restores_visibility(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_b.cookies)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )
    await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        f"/api/reviews/{review_id}", cookies=user_a.cookies
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_list_blocked(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/block", cookies=user_a.cookies
    )
    r = await async_client_integration.get(
        "/api/users/me/blocked", cookies=user_a.cookies
    )
    assert r.status_code == 200
    ids = [u["id"] for u in r.json()["items"]]
    assert user_b.user_id in ids


# ----- mute ------------------------------------------------------------


@pytest.mark.asyncio
async def test_mute_is_idempotent(async_client_integration, user_a, user_b):
    first = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )
    second = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["muted"] is True


@pytest.mark.asyncio
async def test_mute_hides_review_from_muter_feed(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_b.cookies)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        "/api/feed?type=following&limit=50", cookies=user_a.cookies
    )
    assert all(it["id"] != review_id for it in r.json()["items"])


@pytest.mark.asyncio
async def test_mute_unidirectional_does_not_hide_muter_from_muted(
    async_client_integration, user_a, user_b
):
    """A muteó a B. La review de A SÍ debe seguir apareciendo en el
    feed de B (mute es silencioso y unidireccional)."""
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_b.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        "/api/feed?type=following&limit=50", cookies=user_b.cookies
    )
    assert any(it["id"] == review_id for it in r.json()["items"])


@pytest.mark.asyncio
async def test_mute_suppresses_notification(
    async_client_integration, user_a, user_b
):
    """A muteó a B. B comenta la review de A. A no recibe notif."""
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )

    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "comment de muteado"},
        cookies=user_b.cookies,
    )
    assert r.status_code in (200, 201)

    notifs = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_a.cookies
        )
    ).json()["items"]
    assert all(
        n.get("actor", {}).get("id") != user_b.user_id
        and n.get("actor_user_id") != user_b.user_id
        for n in notifs
    )


@pytest.mark.asyncio
async def test_self_mute_rejected(async_client_integration, user_a):
    r = await async_client_integration.post(
        f"/api/users/{user_a.user_id}/mute", cookies=user_a.cookies
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unmute_is_idempotent(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )
    first = await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )
    second = await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["muted"] is False


@pytest.mark.asyncio
async def test_list_muted(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/mute", cookies=user_a.cookies
    )
    r = await async_client_integration.get(
        "/api/users/me/muted", cookies=user_a.cookies
    )
    assert r.status_code == 200
    ids = [u["id"] for u in r.json()["items"]]
    assert user_b.user_id in ids
