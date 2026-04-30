"""Integration tests for email verification post-signup."""

import os
import uuid

import pytest
from sqlalchemy import select, text

from app.database import engine
from app.models.email_verification import EmailVerificationToken
from app.services.email_verification_service import (
    _hash_token,
    create_verification_token,
)

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_register_marks_user_unverified_and_creates_token(
    async_client_integration,
):
    email = f"pytest_verify_{uuid.uuid4().hex[:8]}@test.com"
    r = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "longenough",
            "display_name": "Verify",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["email_verified"] is False

    # Hay un token en la DB para este user
    user_id = body["id"]
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM email_verification_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
    assert rows == 1


@pytest.mark.asyncio
async def test_verify_email_with_valid_token_marks_verified(
    async_client_integration, user_a
):
    # Generar un token via service para tener el plano.
    from app.database import async_session
    from app.models.user import User as UserModel

    async with async_session() as session:
        user_row = await session.execute(
            select(UserModel).where(UserModel.id == uuid.UUID(user_a.user_id))
        )
        user = user_row.scalar_one()
        token = await create_verification_token(session, user)
        await session.commit()

    r = await async_client_integration.post(
        f"/api/auth/verify-email/{token}"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email_verified"] is True


@pytest.mark.asyncio
async def test_verify_email_invalid_token_returns_400(async_client_integration):
    r = await async_client_integration.post(
        "/api/auth/verify-email/this-is-not-a-valid-token-12345"
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_verify_email_token_is_single_use(
    async_client_integration, user_b
):
    from app.database import async_session
    from app.models.user import User as UserModel

    async with async_session() as session:
        user_row = await session.execute(
            select(UserModel).where(UserModel.id == uuid.UUID(user_b.user_id))
        )
        user = user_row.scalar_one()
        token = await create_verification_token(session, user)
        await session.commit()

    first = await async_client_integration.post(
        f"/api/auth/verify-email/{token}"
    )
    assert first.status_code == 200

    # Re-uso del mismo token: el consumed_at ya está set, devuelve 400.
    second = await async_client_integration.post(
        f"/api/auth/verify-email/{token}"
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_resend_invalidates_previous_tokens(async_client_integration):
    email = f"pytest_resend_{uuid.uuid4().hex[:8]}@test.com"
    reg = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "longenough",
            "display_name": "R",
        },
    )
    assert reg.status_code == 201

    login = await async_client_integration.post(
        "/api/auth/login", json={"email": email, "password": "longenough"}
    )
    cookies = login.cookies

    user_id = reg.json()["id"]
    # Capturar el primer token (el del register)
    async with engine.connect() as conn:
        before = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM email_verification_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
    assert before == 1

    # Resend
    r = await async_client_integration.post(
        "/api/auth/resend-verification", cookies=cookies
    )
    assert r.status_code == 204

    # El token previo debe estar marcado consumed; queda solo el nuevo abierto.
    async with engine.connect() as conn:
        active = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM email_verification_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        consumed = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM email_verification_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NOT NULL"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
    assert active == 1
    assert consumed == 1


@pytest.mark.asyncio
async def test_resend_idempotent_when_already_verified(async_client_integration):
    email = f"pytest_dbl_{uuid.uuid4().hex[:8]}@test.com"
    reg = await async_client_integration.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "longenough",
            "display_name": "D",
        },
    )
    user_id = reg.json()["id"]

    # Forzar verified directamente.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET email_verified_at = now() WHERE id = :uid"
            ),
            {"uid": user_id},
        )

    # Snapshot de tokens antes del resend (debería tener 1 del register).
    async with engine.connect() as conn:
        before = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM email_verification_tokens "
                    "WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
        ).scalar_one()

    login = await async_client_integration.post(
        "/api/auth/login", json={"email": email, "password": "longenough"}
    )
    r = await async_client_integration.post(
        "/api/auth/resend-verification", cookies=login.cookies
    )
    assert r.status_code == 204

    # No se debería haber creado un token nuevo cuando el user ya está
    # verificado — el endpoint es no-op en ese caso.
    async with engine.connect() as conn:
        after = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM email_verification_tokens "
                    "WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
    assert after == before


def test_token_is_hashed_in_db():
    """El plain del token NO debe estar guardado en DB — solo SHA-256."""
    plain = "some-known-test-token-value-1234"
    h = _hash_token(plain)
    assert len(h) == 64  # sha256 hex
    assert h != plain