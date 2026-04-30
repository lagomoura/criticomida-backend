"""Integration tests for rate limiting and comment anti-spam (PEND-2)."""

import pytest

from tests.integration.conftest import create_review


@pytest.fixture(autouse=True)
def _enable_rate_limiter():
    """Activate the limiter only in this module and reset buckets per test."""
    from app.middleware.rate_limit import limiter

    previous = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()
        limiter.enabled = previous


async def test_comment_create_rate_limit(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_b.cookies)
    for i in range(5):
        r = await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": f"Comentario {i}"},
            cookies=user_a.cookies,
        )
        assert r.status_code == 201, r.text

    r6 = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": "Comentario 6"},
        cookies=user_a.cookies,
    )
    assert r6.status_code == 429, r6.text


async def test_like_rate_limit(async_client_integration, user_a, user_b):
    # Likes are idempotent, so hitting the same review 61 times works for
    # asserting the limiter kicks in without flooding `/api/posts`.
    review_id = await create_review(async_client_integration, user_b.cookies)

    for _ in range(60):
        r = await async_client_integration.post(
            f"/api/reviews/{review_id}/like", cookies=user_a.cookies
        )
        assert r.status_code == 200, r.text

    r61 = await async_client_integration.post(
        f"/api/reviews/{review_id}/like", cookies=user_a.cookies
    )
    assert r61.status_code == 429, r61.text


async def test_follow_rate_limit(
    async_client_integration, integration_app, user_a
):
    from tests.integration.conftest import register_and_login
    import httpx

    targets = [
        await register_and_login(async_client_integration) for _ in range(31)
    ]

    # register_and_login reusa el client compartido y deja sus cookies como
    # las del último target registrado. Pasar `cookies=` request-level se
    # mezcla con esas cookies sticky (httpx warning dixit), lo que termina
    # mandando el access_token de target30 en lugar del de user_a, y los
    # 30 follows caen en el bucket equivocado.
    #
    # Solución: cliente fresco con `cookies=` en el constructor (cookies
    # del jar inicial, sin sticky residue). Es el mismo integration_app —
    # comparte la DB y los usuarios ya creados.
    transport = httpx.ASGITransport(app=integration_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies=user_a.cookies
    ) as client:
        for t in targets[:30]:
            r = await client.post(f"/api/users/{t.user_id}/follow")
            assert r.status_code == 200, r.text

        r31 = await client.post(f"/api/users/{targets[30].user_id}/follow")
        assert r31.status_code == 429, r31.text


async def test_comment_duplicate_body_blocked(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_b.cookies)
    body = "Mismo mensaje repetido"
    for _ in range(2):
        r = await async_client_integration.post(
            f"/api/reviews/{review_id}/comments",
            json={"body": body},
            cookies=user_a.cookies,
        )
        assert r.status_code == 201, r.text

    # 3rd identical body within the window → anti-spam 429
    # (slowapi allows it because only 3 comments used of the 5/min budget).
    r3 = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": body},
        cookies=user_a.cookies,
    )
    assert r3.status_code == 429, r3.text


async def test_comment_too_many_urls_blocked(async_client_integration, user_a, user_b):
    review_id = await create_review(async_client_integration, user_b.cookies)
    body = "Mirá: https://a.com https://b.com https://c.com https://d.com"
    r = await async_client_integration.post(
        f"/api/reviews/{review_id}/comments",
        json={"body": body},
        cookies=user_a.cookies,
    )
    assert r.status_code == 400, r.text
    assert "enlaces" in r.json()["detail"].lower()
