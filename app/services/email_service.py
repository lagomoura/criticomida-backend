"""Servicio de email transaccional con Resend.

Diseño:
- Dry-run cuando RESEND_API_KEY está vacío. Loguea el payload en lugar de
  enviar, permitiendo que dev y staging corran sin cuenta del proveedor.
- Errores HTTP no bloquean el flujo del caller — solo los loguea. La razón
  es que un email caído (proveedor down, dominio no verificado) no debería
  hacer fallar un approve/reject de claim.
- Plantillas HTML inline mínimas. Cuando crezca, mover a Jinja2 o un
  template engine dedicado.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


_RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
) -> bool:
    """Envía un email vía Resend. Devuelve True si se enviό OK (o si fue
    dry-run), False si el provider devolvió error.

    Falla *silenciosa* por diseño: nunca propaga la excepción al caller. Los
    intentos fallidos quedan en logs estructurados para debug."""
    payload: dict[str, Any] = {
        "from": settings.EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text is not None:
        payload["text"] = text

    if not settings.RESEND_API_KEY:
        # WARNING en lugar de INFO para que sea visible en dev sin tocar el
        # config global de logging — es operacionalmente importante saber
        # que un email transaccional NO se envió.
        logger.warning(
            "email.dry_run to=%s subject=%r body_chars=%d (set RESEND_API_KEY to send)",
            to,
            subject,
            len(html),
        )
        return True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                _RESEND_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        if r.status_code >= 400:
            logger.warning(
                "email.send_failed to=%s status=%d body=%s",
                to,
                r.status_code,
                r.text[:300],
            )
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning("email.send_exception to=%s err=%s", to, exc)
        return False


# ── Templates ───────────────────────────────────────────────────────────────


def _wrap(body_html: str) -> str:
    """Layout HTML mínimo. Inline styles porque la mayoría de los clientes
    de email ignoran <style>."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CritiComida</title></head>
<body style="margin:0;background:#faf7f2;font-family:Arial,sans-serif;color:#2a2520;">
  <div style="max-width:560px;margin:0 auto;padding:32px 24px;">
    <h1 style="font-size:24px;color:#a04a3c;margin:0 0 24px;">CritiComida</h1>
    {body_html}
    <hr style="border:none;border-top:1px solid #e8e0d4;margin:32px 0 16px;">
    <p style="font-size:12px;color:#8b8076;margin:0;">
      Este email fue enviado automáticamente. Si tenés alguna duda escribinos
      a soporte respondiendo este correo.
    </p>
  </div>
</body></html>"""


def render_claim_approved(
    restaurant_name: str, restaurant_slug: str
) -> tuple[str, str, str]:
    panel_url = f"{settings.PUBLIC_APP_URL}/restaurants/{restaurant_slug}/owner"
    subject = f"Tu reclamo de {restaurant_name} fue aprobado"
    html = _wrap(
        f"""
    <p style="font-size:16px;line-height:1.5;">
      ¡Listo! Verificamos que sos el dueño de
      <strong>{restaurant_name}</strong> y desbloqueamos las herramientas
      del panel: respondé reseñas, subí fotos oficiales y mantené la ficha
      al día.
    </p>
    <p style="margin-top:24px;">
      <a href="{panel_url}"
         style="display:inline-block;background:#a04a3c;color:#fff;
                padding:12px 20px;border-radius:8px;text-decoration:none;
                font-weight:600;">
        Ir al panel del restaurante
      </a>
    </p>
    """
    )
    text = (
        f"Aprobamos tu reclamo de {restaurant_name}. Entrá al panel: {panel_url}"
    )
    return subject, html, text


def render_claim_rejected(
    restaurant_name: str, reason: str
) -> tuple[str, str, str]:
    subject = f"Tu reclamo de {restaurant_name} fue rechazado"
    html = _wrap(
        f"""
    <p style="font-size:16px;line-height:1.5;">
      Revisamos tu reclamo de <strong>{restaurant_name}</strong> y por ahora
      no pudimos aprobarlo. Motivo:
    </p>
    <blockquote style="border-left:3px solid #a04a3c;padding:8px 16px;
                       margin:16px 0;background:#fff5ef;color:#5a4a40;">
      {reason}
    </blockquote>
    <p style="font-size:14px;color:#5a4a40;">
      Si creés que hay un error podés volver a reclamar después de 30 días o
      escribirnos respondiendo este correo con evidencia adicional.
    </p>
    """
    )
    text = (
        f"Tu reclamo de {restaurant_name} fue rechazado. Motivo: {reason}"
    )
    return subject, html, text


def render_claim_revoked(
    restaurant_name: str, reason: str
) -> tuple[str, str, str]:
    subject = f"Revocamos tu verificación de {restaurant_name}"
    html = _wrap(
        f"""
    <p style="font-size:16px;line-height:1.5;">
      Tu verificación como dueño de <strong>{restaurant_name}</strong> fue
      revocada por el equipo de moderación. Motivo:
    </p>
    <blockquote style="border-left:3px solid #a04a3c;padding:8px 16px;
                       margin:16px 0;background:#fff5ef;color:#5a4a40;">
      {reason}
    </blockquote>
    <p style="font-size:14px;color:#5a4a40;">
      Si pensás que es un error, escribinos respondiendo este email para
      revisar la situación.
    </p>
    """
    )
    text = (
        f"Revocamos tu verificación de {restaurant_name}. Motivo: {reason}"
    )
    return subject, html, text
