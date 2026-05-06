"""Integration tests for @mention notifications and the mention-search
endpoint."""

import os
import uuid

import httpx
import pytest

from tests.integration.conftest import (
    RegisteredUser,
    create_review,
    register_and_login,
)


if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )


pytestmark = pytest.mark.integration


async def _set_handle(
    client: httpx.AsyncClient, user: RegisteredUser, handle: str
) -> None:
    """Setea ``handle`` en /api/users/me. Reusa el endpoint público porque no
    queremos bypasear validación."""
    r = await client.patch(
        "/api/users/me", json={"handle": handle}, cookies=user.cookies
    )
    assert r.status_code == 200, r.text


async def _list_notifications(
    client: httpx.AsyncClient, cookies
) -> list[dict]:
    r = await client.get("/api/notifications", cookies=cookies)
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.mark.asyncio
async def test_mention_in_comment_creates_notification(
    async_client_integration, user_a, user_b
):
    """User A escribe una review. User B comenta arrobando a User A → A
    recibe SOLO la notif ``comment`` (la de mención se dedupea contra el
    autor). Repetimos con un tercer usuario C arrobado: C debe recibir
    ``mention``."""
    handle_a = f"pyt{uuid.uuid4().hex[:8]}"
    await _set_handle(async_client_integration, user_a, handle_a)

    user_c: RegisteredUser = await register_and_login(async_client_integration)
    handle_c = f"pyt{uuid.uuid4().hex[:8]}"
    await _set_handle(async_client_integration, user_c, handle_c)

    review_id = await create_review(async_client_integration, user_a.cookies)

    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": f"hola @{handle_a} @{handle_c} qué tal"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 201, r.text

    # User A: tiene 'comment', NO tiene 'mention' (skip por ser autor de la review).
    notifs_a = await _list_notifications(async_client_integration, user_a.cookies)
    kinds_a = [n["kind"] for n in notifs_a]
    assert "comment" in kinds_a
    assert "mention" not in kinds_a

    # User C: tiene 'mention'.
    notifs_c = await _list_notifications(async_client_integration, user_c.cookies)
    kinds_c = [n["kind"] for n in notifs_c]
    assert "mention" in kinds_c
    mention_notif = next(n for n in notifs_c if n["kind"] == "mention")
    assert "te mencion" in mention_notif["text"]


@pytest.mark.asyncio
async def test_self_mention_is_skipped(
    async_client_integration, user_a, user_b
):
    """User B se arroba a sí mismo en un comentario → no aparece notif."""
    handle_b = f"pyt{uuid.uuid4().hex[:8]}"
    await _set_handle(async_client_integration, user_b, handle_b)

    review_id = await create_review(async_client_integration, user_a.cookies)

    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": f"hola @{handle_b}"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 201, r.text

    notifs_b = await _list_notifications(async_client_integration, user_b.cookies)
    assert all(n["kind"] != "mention" for n in notifs_b)


@pytest.mark.asyncio
async def test_unknown_handle_does_not_break_submit(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "hola @nonexistent_handle_xyz"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_email_in_body_does_not_trigger_mention(
    async_client_integration, user_a, user_b
):
    """Anti-email regex: ``foo@bar`` adentro de un email NO matchea."""
    user_c: RegisteredUser = await register_and_login(async_client_integration)
    handle_c = f"bar{uuid.uuid4().hex[:6]}"
    # Setear @bar... como handle de C.
    await _set_handle(async_client_integration, user_c, handle_c)

    review_id = await create_review(async_client_integration, user_a.cookies)
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": f"escribime a foo@{handle_c}.com"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 201, r.text

    notifs_c = await _list_notifications(async_client_integration, user_c.cookies)
    assert all(n["kind"] != "mention" for n in notifs_c)


@pytest.mark.asyncio
async def test_mention_search_excludes_users_without_handle(
    async_client_integration, user_a
):
    """User_a (sin handle) no debe aparecer al buscar prefijo común. Ese es
    el contrato del endpoint: solo handles seteados."""
    # Aseguramos que A NO tiene handle (es lo default tras register).
    me = await async_client_integration.get(
        "/api/users/me", cookies=user_a.cookies
    )
    if me.status_code == 200:
        # Si por alguna razón el seed o un test anterior le puso handle,
        # skipear este test antes que ensuciar el setup.
        if me.json().get("handle"):
            pytest.skip("user_a already has a handle; skipping clean-state assertion")

    r = await async_client_integration.get(
        "/api/users/mention-search?q=pyt", cookies=user_a.cookies
    )
    assert r.status_code == 200, r.text
    handles = [u["handle"] for u in r.json()]
    # Ningún resultado puede tener handle nulo y ningún resultado puede ser
    # el propio user_a.
    assert all(h is not None for h in handles)
    user_ids = [u["id"] for u in r.json()]
    assert user_a.user_id not in user_ids


@pytest.mark.asyncio
async def test_mention_search_prefix_match(
    async_client_integration, user_a
):
    user_b: RegisteredUser = await register_and_login(async_client_integration)
    unique_prefix = f"zzz{uuid.uuid4().hex[:6]}"
    await _set_handle(async_client_integration, user_b, unique_prefix)

    # Prefix exacto.
    r = await async_client_integration.get(
        f"/api/users/mention-search?q={unique_prefix[:3]}",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    handles = [u["handle"] for u in r.json()]
    assert any(h == unique_prefix for h in handles)


@pytest.mark.asyncio
async def test_mention_search_strips_leading_at(
    async_client_integration, user_a
):
    user_b: RegisteredUser = await register_and_login(async_client_integration)
    unique = f"yyy{uuid.uuid4().hex[:6]}"
    await _set_handle(async_client_integration, user_b, unique)

    r = await async_client_integration.get(
        f"/api/users/mention-search?q=@{unique[:3]}",
        cookies=user_a.cookies,
    )
    assert r.status_code == 200
    assert any(u["handle"] == unique for u in r.json())
