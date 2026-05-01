"""Integration tests for comments router."""

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
async def test_create_and_list_comments(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)

    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "Qué bien se ve!"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 201
    created = r.json()
    assert created["body"] == "Qué bien se ve!"
    assert created["author"]["id"] == user_b.user_id

    r = await async_client_integration.get(
        f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_comment_permissions_flags(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    # user_b comments
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "from b"},
            cookies=user_b.cookies,
        )
    ).json()

    # user_a (not author of comment) sees can_delete=False, can_report=True
    items_a = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_a.cookies
        )
    ).json()["items"]
    mine_as_a = next(i for i in items_a if i["id"] == created["id"])
    assert mine_as_a["can_delete"] is False
    assert mine_as_a["can_report"] is True

    # user_b (author) sees can_delete=True, can_report=False
    items_b = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
        )
    ).json()["items"]
    mine_as_b = next(i for i in items_b if i["id"] == created["id"])
    assert mine_as_b["can_delete"] is True
    assert mine_as_b["can_report"] is False


@pytest.mark.asyncio
async def test_only_author_or_admin_can_delete(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "to delete"},
            cookies=user_b.cookies,
        )
    ).json()

    # user_a (not the commenter) → 403
    r = await async_client_integration.delete(
        f"/api/comments/{created['id']}", cookies=user_a.cookies
    )
    assert r.status_code == 403

    # user_b (commenter) → 204
    r = await async_client_integration.delete(
        f"/api/comments/{created['id']}", cookies=user_b.cookies
    )
    assert r.status_code == 204

    # Soft-deleted: list should not include it anymore.
    items = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_b.cookies
        )
    ).json()["items"]
    assert all(i["id"] != created["id"] for i in items)


@pytest.mark.asyncio
async def test_edit_own_comment(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "primera version"},
            cookies=user_b.cookies,
        )
    ).json()

    r = await async_client_integration.patch(
        f"/api/comments/{created['id']}",
        json={"body": "version corregida"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 200
    edited = r.json()
    assert edited["body"] == "version corregida"
    assert edited["can_edit"] is True
    assert edited["updated_at"] >= created["updated_at"]


@pytest.mark.asyncio
async def test_only_author_can_edit(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "ajeno"},
            cookies=user_b.cookies,
        )
    ).json()

    # user_a is not the author
    r = await async_client_integration.patch(
        f"/api/comments/{created['id']}",
        json={"body": "intento de hijack"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_edit_missing_or_deleted_comment_404(
    async_client_integration, user_a, user_b
):
    # Missing
    r = await async_client_integration.patch(
        f"/api/comments/{uuid.uuid4()}",
        json={"body": "nope"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 404

    # Soft-deleted
    review_id = await create_review(async_client_integration, user_a.cookies)
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "to delete"},
            cookies=user_b.cookies,
        )
    ).json()
    await async_client_integration.delete(
        f"/api/comments/{created['id']}", cookies=user_b.cookies
    )
    r = await async_client_integration.patch(
        f"/api/comments/{created['id']}",
        json={"body": "ressuscitar"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edit_validates_body_length(async_client_integration, user_a):
    review_id = await create_review(async_client_integration, user_a.cookies)
    created = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "ok"},
            cookies=user_a.cookies,
        )
    ).json()

    r = await async_client_integration.patch(
        f"/api/comments/{created['id']}",
        json={"body": ""},
        cookies=user_a.cookies,
    )
    assert r.status_code == 422

    r = await async_client_integration.patch(
        f"/api/comments/{created['id']}",
        json={"body": "x" * 501},
        cookies=user_a.cookies,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_comment_on_missing_review_404(
    async_client_integration, user_a
):
    missing = uuid.uuid4()
    r = await async_client_integration.post(
        f"/api/reviews/{missing}/comments",
        json={"body": "ghost"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_create_comment_requires_auth(async_client_integration, user_a):
    review_id = await create_review(async_client_integration, user_a.cookies)
    async_client_integration.cookies.clear()
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments", json={"body": "hi"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_comment_generates_notification(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "buenísimo, voy mañana"},
        cookies=user_b.cookies,
    )
    notifs = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_a.cookies
        )
    ).json()["items"]
    assert any(
        n["kind"] == "comment" and n["target_review_id"] == review_id
        for n in notifs
    )


# ── Replies (1 nivel de anidación) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_reply_and_list(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    parent = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "comentario padre"},
            cookies=user_b.cookies,
        )
    ).json()

    r = await async_client_integration.post(
        f"/api/comments/{parent['id']}/replies",
        json={"body": "respuesta!"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 201
    reply = r.json()
    assert reply["parent_comment_id"] == parent["id"]
    assert reply["body"] == "respuesta!"

    # Listar el review NO debe traer la reply (sólo top-level).
    items = (
        await async_client_integration.get(
            f"/api/reviews/{review_id}/comments", cookies=user_a.cookies
        )
    ).json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == parent["id"]
    assert items[0]["replies_count"] == 1

    # GET /api/comments/{id}/replies sí lista la reply.
    replies = (
        await async_client_integration.get(
            f"/api/comments/{parent['id']}/replies", cookies=user_a.cookies
        )
    ).json()["items"]
    assert len(replies) == 1
    assert replies[0]["id"] == reply["id"]


@pytest.mark.asyncio
async def test_cannot_reply_to_a_reply(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_a.cookies)
    parent = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "padre"},
            cookies=user_b.cookies,
        )
    ).json()
    reply = (
        await async_client_integration.post(
            f"/api/comments/{parent['id']}/replies",
            json={"body": "respuesta nivel 1"},
            cookies=user_a.cookies,
        )
    ).json()

    r = await async_client_integration.post(
        f"/api/comments/{reply['id']}/replies",
        json={"body": "nivel 2 prohibido"},
        cookies=user_b.cookies,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_cannot_reply_to_missing_or_deleted_comment(
    async_client_integration, user_a, user_b
):
    r = await async_client_integration.post(
        f"/api/comments/{uuid.uuid4()}/replies",
        json={"body": "ghost"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 404

    review_id = await create_review(async_client_integration, user_a.cookies)
    parent = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "se borra"},
            cookies=user_b.cookies,
        )
    ).json()
    await async_client_integration.delete(
        f"/api/comments/{parent['id']}", cookies=user_b.cookies
    )
    r = await async_client_integration.post(
        f"/api/comments/{parent['id']}/replies",
        json={"body": "tarde"},
        cookies=user_a.cookies,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reply_notifies_parent_author_and_review_owner(
    async_client_integration, user_a, user_b
):
    review_id = await create_review(async_client_integration, user_a.cookies)
    parent = (
        await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": "padre"},
            cookies=user_b.cookies,
        )
    ).json()
    await async_client_integration.post(
        f"/api/comments/{parent['id']}/replies",
        json={"body": "respondo"},
        cookies=user_a.cookies,
    )
    # El autor del comentario padre (user_b) recibe comment_reply.
    notifs_b = (
        await async_client_integration.get(
            "/api/notifications", cookies=user_b.cookies
        )
    ).json()["items"]
    assert any(n["kind"] == "comment_reply" for n in notifs_b)
    # El autor de la reseña (user_a) que también respondió no se autonotifica:
    # como la reply la creó user_a, ni el comment ni el comment_reply le llegan
    # a sí mismo.
