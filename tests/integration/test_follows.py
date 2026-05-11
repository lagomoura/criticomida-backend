"""Integration tests for the follows router."""

import os

import pytest

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 and a reachable DATABASE_URL "
        "to run integration tests",
        allow_module_level=True,
    )

from tests.integration.conftest import register_and_login  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_follow_increments_counter(async_client_integration, user_a, user_b):
    r = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["following"] is True
    assert body["followers_count"] >= 1


@pytest.mark.asyncio
async def test_follow_is_idempotent(async_client_integration, user_a, user_b):
    first = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    second = await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    assert second.status_code == 200
    # Counter should not double on a repeat follow.
    assert second.json()["followers_count"] == first.json()["followers_count"]


@pytest.mark.asyncio
async def test_self_follow_rejected(async_client_integration, user_a):
    r = await async_client_integration.post(
        f"/api/users/{user_a.user_id}/follow", cookies=user_a.cookies
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unfollow_is_idempotent(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    first = await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    second = await async_client_integration.delete(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["following"] is False


@pytest.mark.asyncio
async def test_follow_requires_auth(async_client_integration, user_b):
    # The shared client persists cookies from earlier logins; clear them to
    # simulate an unauthenticated request.
    async_client_integration.cookies.clear()
    r = await async_client_integration.post(f"/api/users/{user_b.user_id}/follow")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_followers_paginates(async_client_integration, user_a, user_b):
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    r = await async_client_integration.get(
        f"/api/users/{user_b.user_id}/followers?limit=1"
    )
    assert r.status_code == 200
    body = r.json()
    assert any(item["id"] == user_a.user_id for item in body["items"])


@pytest.mark.asyncio
async def test_list_followers_anonymous_omits_viewer_following(
    async_client_integration, user_a, user_b
):
    """Sin sesión, cada item viene con viewer_following=None.

    Garantiza que un usuario anónimo (o no autenticado) no recibe
    información sobre el grafo de seguimiento de nadie.
    """
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    async_client_integration.cookies.clear()
    r = await async_client_integration.get(
        f"/api/users/{user_b.user_id}/followers"
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) >= 1
    for item in body["items"]:
        assert item["viewer_following"] is None


@pytest.mark.asyncio
async def test_list_followers_with_viewer_marks_who_viewer_follows(
    async_client_integration, user_a, user_b
):
    """Viewer autenticado: marca True solo a los que el viewer ya sigue.

    Setup: user_a y user_c siguen a user_b. user_a (viewer) consulta
    followers(user_b). Verifica que se ve a sí mismo con viewer_following=False
    (no se sigue a sí mismo) y que user_c viene con True si user_a lo sigue,
    o False si no.
    """
    user_c = await register_and_login(async_client_integration)

    # Setup graph:
    # - user_a → follows user_b   (so user_a appears in followers of user_b)
    # - user_c → follows user_b   (so user_c appears in followers of user_b)
    # - user_a → follows user_c   (so when user_a queries the list,
    #                              user_c shows viewer_following=True)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_c.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_c.user_id}/follow", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        f"/api/users/{user_b.user_id}/followers",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    items = {item["id"]: item for item in r.json()["items"]}
    assert user_a.user_id in items
    assert user_c.user_id in items
    # Cannot self-follow, so a viewer seeing themselves is always False.
    assert items[user_a.user_id]["viewer_following"] is False
    # user_a does follow user_c → True.
    assert items[user_c.user_id]["viewer_following"] is True


@pytest.mark.asyncio
async def test_list_following_self_marks_all_true(
    async_client_integration, user_a, user_b
):
    """Cuando uno consulta su propia /following, todos vienen True.

    Por definición: si X está en la lista de "following" de user_a, user_a
    sigue a X. Así que el flag viewer_following debe ser True para todos
    los items cuando el viewer coincide con el dueño de la lista.
    """
    user_c = await register_and_login(async_client_integration)
    await async_client_integration.post(
        f"/api/users/{user_b.user_id}/follow", cookies=user_a.cookies
    )
    await async_client_integration.post(
        f"/api/users/{user_c.user_id}/follow", cookies=user_a.cookies
    )

    r = await async_client_integration.get(
        f"/api/users/{user_a.user_id}/following",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 2
    for item in items:
        assert item["viewer_following"] is True, item
