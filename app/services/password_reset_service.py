"""Forgot password / reset password flow.

Mismas reglas de seguridad que email_verification_service:
- Token plano solo en email; en DB queda SHA-256.
- Single-use con consumed_at.
- TTL más corto (60 min) porque un token de reset es más sensible.
- Reset password revoca todos los refresh tokens del user — si la cuenta
  estaba comprometida, las sesiones activas del atacante quedan
  inutilizadas en el próximo refresh.
- /forgot-password devuelve 204 siempre (no leak de cuáles emails están
  registrados).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.middleware.auth import hash_password
from app.models.password_reset import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.email_service import _wrap, send_email


logger = logging.getLogger(__name__)


_TOKEN_TTL_MINUTES = 60


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def request_password_reset(
    db: AsyncSession, *, email: str
) -> None:
    """Genera token + envía email si el email existe. Siempre completa OK
    desde el punto de vista del caller (sin levantar excepciones por user
    inexistente) para no leak existence en el endpoint público."""
    normalized = email.strip().lower()
    rows = await db.execute(select(User).where(User.email == normalized))
    user = rows.scalar_one_or_none()
    if user is None:
        return

    now = datetime.now(timezone.utc)
    # Invalidar tokens previos no consumidos del mismo user.
    await db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )

    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=now + timedelta(minutes=_TOKEN_TTL_MINUTES),
        )
    )
    await db.flush()

    reset_url = f"{settings.PUBLIC_APP_URL}/reset-password/{token}"
    subject = "Recuperá tu contraseña en CritiComida"
    html = _wrap(
        f"""
    <p style="font-size:16px;line-height:1.5;">
      Hola {user.display_name}, recibimos una solicitud para resetear la
      contraseña de tu cuenta. Si fuiste vos, hacé click en el botón:
    </p>
    <p style="margin-top:24px;">
      <a href="{reset_url}"
         style="display:inline-block;background:#a04a3c;color:#fff;
                padding:12px 20px;border-radius:8px;text-decoration:none;
                font-weight:600;">
        Resetear mi contraseña
      </a>
    </p>
    <p style="font-size:14px;color:#5a4a40;margin-top:24px;">
      El link expira en {_TOKEN_TTL_MINUTES} minutos. Si no fuiste vos,
      ignorá este mensaje y tu contraseña sigue intacta.
    </p>
    """
    )
    text = (
        f"Reseteá tu contraseña en CritiComida (link válido {_TOKEN_TTL_MINUTES} min): "
        f"{reset_url}"
    )
    await send_email(to=user.email, subject=subject, html=html, text=text)


async def reset_password_with_token(
    db: AsyncSession, *, token: str, new_password: str
) -> User | None:
    """Valida token, cambia el password_hash y revoca refresh tokens del
    user. Devuelve el User si OK, None si el token es inválido."""
    if not token or len(token) < 16:
        return None

    rows = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_token(token)
        )
    )
    row = rows.scalar_one_or_none()
    if row is None:
        return None

    now = datetime.now(timezone.utc)
    if row.consumed_at is not None or row.expires_at <= now:
        return None

    user_row = await db.execute(select(User).where(User.id == row.user_id))
    user = user_row.scalar_one_or_none()
    if user is None:
        return None

    user.password_hash = hash_password(new_password)
    row.consumed_at = now

    # Invalidar todas las sesiones activas: si la cuenta estaba secuestrada,
    # los refresh tokens del atacante quedan inválidos.
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )

    await db.flush()
    return user
