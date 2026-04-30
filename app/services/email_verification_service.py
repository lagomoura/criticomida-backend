"""Email verification post-signup.

Diseño:
- El token plano se entrega solo en el email. La DB guarda SHA-256 del token,
  así un dump filtrado no expone tokens activos.
- TTL de 24h. El consume marca `consumed_at` para hacerlo single-use.
- Reusar la abstracción de email_service para enviar el mensaje.
- No bloquea login ni acciones del usuario — el front muestra un banner si
  email_verified_at sigue null.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.email_verification import EmailVerificationToken
from app.models.user import User
from app.services.email_service import _wrap, send_email


logger = logging.getLogger(__name__)


_TOKEN_TTL_HOURS = 24


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_verification_token(
    db: AsyncSession, user: User
) -> str:
    """Genera un token nuevo, lo guarda hasheado y devuelve el plain (solo
    para usar dentro del flujo del request — nunca lo persistir).

    Invalida tokens previos no consumidos del mismo user marcándolos como
    consumed_at, así un user que pide "reenviar" deja inválida la versión
    anterior."""
    now = datetime.now(timezone.utc)
    await db.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    token = secrets.token_urlsafe(32)
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=now + timedelta(hours=_TOKEN_TTL_HOURS),
        )
    )
    await db.flush()
    return token


async def consume_verification_token(
    db: AsyncSession, token: str
) -> User | None:
    """Valida el token, marca consumed_at y setea email_verified_at en el user.

    Devuelve el User si OK, None si el token no existe / expiró / ya se usó.
    El caller decide qué status code devolver (404 vs 410 vs 200)."""
    if not token or len(token) < 16:
        return None

    rows = await db.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == _hash_token(token)
        )
    )
    row = rows.scalar_one_or_none()
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    if row.consumed_at is not None:
        return None
    if row.expires_at <= now:
        return None

    user_row = await db.execute(select(User).where(User.id == row.user_id))
    user = user_row.scalar_one_or_none()
    if user is None:
        return None

    row.consumed_at = now
    if user.email_verified_at is None:
        user.email_verified_at = now
    await db.flush()
    return user


async def send_verification_email(user: User, token: str) -> None:
    """Compone el email transaccional y lo dispara. Best-effort — no rompe
    si el provider falla."""
    verify_url = f"{settings.PUBLIC_APP_URL}/verify-email/{token}"
    subject = "Confirmá tu email en CritiComida"
    html = _wrap(
        f"""
    <p style="font-size:16px;line-height:1.5;">
      ¡Hola {user.display_name}! Solo necesitamos confirmar que este es
      tu email para que puedas usar todo CritiComida sin restricciones.
    </p>
    <p style="margin-top:24px;">
      <a href="{verify_url}"
         style="display:inline-block;background:#a04a3c;color:#fff;
                padding:12px 20px;border-radius:8px;text-decoration:none;
                font-weight:600;">
        Confirmar mi email
      </a>
    </p>
    <p style="font-size:14px;color:#5a4a40;margin-top:24px;">
      Si no creaste una cuenta en CritiComida, ignorá este mensaje. El
      link expira en {_TOKEN_TTL_HOURS} horas.
    </p>
    """
    )
    text = (
        f"Confirmá tu email en CritiComida hacé click en este link "
        f"(expira en {_TOKEN_TTL_HOURS}h): {verify_url}"
    )
    await send_email(to=user.email, subject=subject, html=html, text=text)
