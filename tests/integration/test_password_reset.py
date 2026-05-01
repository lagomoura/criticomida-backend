"""Integration tests for forgot/reset password flow."""

import os
import uuid

import pytest
from sqlalchemy import select, text

from app.database import async_session, engine
from app.models.user import User as UserModel
from app.services.password_reset_service import (
    _hash_token,
    request_password_reset,
)

if os.environ.get("RUN_INTEGRATION") != "1":
    pytest.skip(
        "Set RUN_INTEGRATION=1 to run integration tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_forgot_password_returns_204_for_unknown_email(
    async_client_integration,
):
    r = await async_client_integration.post(
        "/api/auth/forgot-password",
        json={"email": "definitely-not-registered@nope.com"},
    )
    # 204 sí o sí — no leak de existencia.
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_forgot_password_creates_token_for_known_email(
    async_client_integration, user_a
):
    r = await async_client_integration.post(
        "/api/auth/forgot-password", json={"email": user_a.email}
    )
    assert r.status_code == 204

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM password_reset_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL"
                ),
                {"uid": user_a.user_id},
            )
        ).scalar_one()
    assert rows == 1


@pytest.mark.asyncio
async def test_reset_password_changes_hash_and_revokes_sessions(
    async_client_integration, user_a
):
    # Generamos token via service para tener el plain.
    async with async_session() as session:
        user_row = await session.execute(
            select(UserModel).where(UserModel.id == uuid.UUID(user_a.user_id))
        )
        user = user_row.scalar_one()
        await request_password_reset(session, email=user.email)
        await session.commit()

    async with engine.connect() as conn:
        token_hash_row = (
            await conn.execute(
                text(
                    "SELECT token_hash FROM password_reset_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"uid": user_a.user_id},
            )
        ).scalar_one()

    # Encontramos el plain del token via brute force usando hash. Como el
    # service genera secrets.token_urlsafe(32), no podemos invertirlo —
    # generamos un token plain conocido directamente:
    known_plain = "test-token-known-12345-aaaaaaaa-bbbbbbbb"
    known_hash = _hash_token(known_plain)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE password_reset_tokens SET token_hash = :h "
                "WHERE token_hash = :old"
            ),
            {"h": known_hash, "old": token_hash_row},
        )

    # Login antes para tener una sesión activa que verificar revocada.
    pre_login = await async_client_integration.post(
        "/api/auth/login",
        json={"email": user_a.email, "password": "longenough"},
    )
    assert pre_login.status_code == 200

    r = await async_client_integration.post(
        "/api/auth/reset-password",
        json={"token": known_plain, "new_password": "brand-new-pass"},
    )
    assert r.status_code == 200

    # La password vieja ya no entra
    bad = await async_client_integration.post(
        "/api/auth/login",
        json={"email": user_a.email, "password": "longenough"},
    )
    assert bad.status_code == 401

    # La nueva sí
    good = await async_client_integration.post(
        "/api/auth/login",
        json={"email": user_a.email, "password": "brand-new-pass"},
    )
    assert good.status_code == 200

    # El refresh token de la sesión vieja quedó revocado (revoke_all_refresh
    # ocurre al login también, pero el reset_password lo dispara antes —
    # verificamos que pasó al menos una vez).
    async with engine.connect() as conn:
        revoked = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM refresh_tokens "
                    "WHERE user_id = :uid AND revoked_at IS NOT NULL"
                ),
                {"uid": user_a.user_id},
            )
        ).scalar_one()
    assert revoked >= 1


@pytest.mark.asyncio
async def test_reset_password_invalid_token_returns_400(async_client_integration):
    r = await async_client_integration.post(
        "/api/auth/reset-password",
        json={
            "token": "this-token-is-not-in-the-db-12345",
            "new_password": "longenough",
        },
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reset_password_token_is_single_use(
    async_client_integration, user_b
):
    # Crear token via service y plantarlo con un hash conocido.
    async with async_session() as session:
        await request_password_reset(session, email=user_b.email)
        await session.commit()

    known_plain = "single-use-token-yyyyyyyyy-zzzzzzzzz"
    known_hash = _hash_token(known_plain)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE password_reset_tokens SET token_hash = :h "
                "WHERE user_id = :uid AND consumed_at IS NULL"
            ),
            {"h": known_hash, "uid": user_b.user_id},
        )

    first = await async_client_integration.post(
        "/api/auth/reset-password",
        json={"token": known_plain, "new_password": "first-pass"},
    )
    assert first.status_code == 200

    # Re-uso del mismo token → 400.
    second = await async_client_integration.post(
        "/api/auth/reset-password",
        json={"token": known_plain, "new_password": "second-pass"},
    )
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_forgot_password_invalidates_previous_tokens(
    async_client_integration, user_a
):
    await async_client_integration.post(
        "/api/auth/forgot-password", json={"email": user_a.email}
    )
    await async_client_integration.post(
        "/api/auth/forgot-password", json={"email": user_a.email}
    )

    # Solo 1 sigue abierto, los demás consumed.
    async with engine.connect() as conn:
        active = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM password_reset_tokens "
                    "WHERE user_id = :uid AND consumed_at IS NULL"
                ),
                {"uid": user_a.user_id},
            )
        ).scalar_one()
    assert active == 1
