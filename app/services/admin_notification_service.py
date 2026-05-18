"""Broadcast helper para notificar a todos los admins.

Por ahora solo dispara el caso ``category_pending_review`` (cuando el
servicio de inferencia auto-crea una categoría nueva), pero la API queda
abierta para futuros eventos admin-only (ej: bulk import fallido,
restaurante reportado por muchos usuarios).

Por qué un helper aparte y no inlinear esto en cada caller:
- DRY: el patrón "query admins → loop → in-app Notification + email" se
  repetiría idéntico en cada caso.
- Idempotencia y best-effort: ambas inserciones (Notification + email)
  toleran fallas individuales — un admin sin email no debe bloquear al
  resto del fanout.
- Test surface: se mockea un solo punto en lugar de N call sites.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.social import Notification
from app.models.user import User, UserRole
from app.services.email_service import (
    render_category_pending_review,
    send_email,
)


logger = logging.getLogger(__name__)


_NOTIFICATION_TEXT_TEMPLATE = (
    'Nueva categoría pendiente "{name}" creada al clasificar el plato "{dish}"'
)


async def _load_admin_users(db: AsyncSession) -> list[User]:
    """Carga todos los users con role=admin. Para Palato esto es un set
    chico (≤ 5) — query directa sin paginar. Si en el futuro crece a
    decenas habrá que paginar y/o batchear el email send."""
    result = await db.execute(
        select(User).where(User.role == UserRole.admin)
    )
    return list(result.scalars().all())


async def notify_admins_category_pending(
    db: AsyncSession,
    category: Category,
    *,
    dish_name: str,
    restaurant_name: str | None = None,
    triggered_by_user_id: uuid.UUID | None = None,
) -> None:
    """Dispara la notificación de cola de pendientes a todos los admins.

    Inserta una fila en ``notifications`` por admin (kind=
    ``category_pending_review``) y manda email transaccional vía Resend.
    Ambas operaciones son best-effort: cualquier falla individual queda
    logueada y NO rompe el flujo del caller (que está creando un post).

    El caller commitea: este helper solo agrega filas a la sesión.
    """
    admins = await _load_admin_users(db)
    if not admins:
        logger.warning(
            "category_pending_review: no admins found in DB (cat=%s)",
            category.slug,
        )
        return

    text_body = _NOTIFICATION_TEXT_TEMPLATE.format(
        name=category.name, dish=dish_name
    )
    if len(text_body) > 500:
        text_body = text_body[:497] + "…"

    # Actor: para el modelo Notification es NOT NULL. Si la categoría se
    # creó por inferencia automática y no tenemos un user disparador
    # claro, usamos al propio admin como actor (auto-notificación). El
    # caso típico es que ``triggered_by_user_id`` venga del request en
    # curso (el autor de la reseña).
    for admin in admins:
        actor_id = triggered_by_user_id or admin.id
        db.add(
            Notification(
                recipient_user_id=admin.id,
                actor_user_id=actor_id,
                kind="category_pending_review",
                text=text_body,
            )
        )

        # Email transaccional. Falla silenciosa por diseño (ver email_service).
        if admin.email:
            subject, html, txt = render_category_pending_review(
                category_name=category.name,
                category_slug=category.slug,
                category_description=category.description,
                dish_name=dish_name,
                restaurant_name=restaurant_name,
            )
            try:
                await send_email(
                    to=str(admin.email),
                    subject=subject,
                    html=html,
                    text=txt,
                )
            except Exception as exc:  # noqa: BLE001 — defensivo
                logger.warning(
                    "category_pending_review email failed admin=%s err=%s",
                    admin.id,
                    exc,
                )

    logger.info(
        "category_pending_review fanout cat=%s admins=%d",
        category.slug,
        len(admins),
    )


async def notify_admins_user_created(
    db: AsyncSession,
    new_user: User,
) -> None:
    """Avisa a todos los admins que se registró un usuario nuevo.

    Solo in-app (sin email): a escala, un mail por cada signup inunda la
    casilla del admin sin aportar — la campanita alcanza. Inserta una fila
    en ``notifications`` por admin (kind=``user_created``) apuntando al
    perfil del usuario nuevo vía ``target_user_id``, de modo que el click
    abre ``/u/{id}``.

    Best-effort por diseño: si no hay admins, loguea y sale sin romper el
    registro. El caller commitea — este helper solo agrega filas a la sesión.

    Nota: el usuario nuevo es su propio ``actor`` (NOT NULL en el modelo);
    es el mismo patrón de auto-notificación que ``category_pending_review``.
    """
    admins = await _load_admin_users(db)
    if not admins:
        logger.warning(
            "user_created: no admins found in DB (user=%s)", new_user.id
        )
        return

    handle = new_user.handle or new_user.display_name
    text_body = f"Nuevo usuario registrado: @{handle}"
    if len(text_body) > 500:
        text_body = text_body[:497] + "…"

    for admin in admins:
        # Un admin no necesita el aviso de su propia alta.
        if admin.id == new_user.id:
            continue
        db.add(
            Notification(
                recipient_user_id=admin.id,
                actor_user_id=new_user.id,
                kind="user_created",
                target_user_id=new_user.id,
                text=text_body,
            )
        )

    logger.info(
        "user_created fanout user=%s admins=%d", new_user.id, len(admins)
    )
