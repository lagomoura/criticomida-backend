"""Extracción y resolución de menciones ``@handle`` en texto libre.

El parser y la resolución viven separados de ``notification_service`` porque
el grafo es: cualquier endpoint que acepte texto del usuario llama a
``extract_handles`` → ``resolve_mention_recipients`` → emite notificaciones.

Reglas:
- Boundary izquierdo ``(?<![A-Za-z0-9_])`` previene match dentro de emails
  (``foo@bar.com`` NO matchea, ``hola @bar`` sí).
- Charset ``[A-Za-z0-9_]`` matchea exactamente lo que valida ``User.handle``.
- Tope de 30 chars por mención evita capturar texto largo accidental.
- Dedup case-insensitive antes de la query (``User.handle`` es CITEXT).
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,30})")


def extract_handles(text: str) -> list[str]:
    """Devuelve los handles arrobados en orden de aparición, sin duplicados
    (case-insensitive). Strings vacíos o ``None`` retornan lista vacía."""
    if not text:
        return []
    seen: set[str] = set()
    handles: list[str] = []
    for match in _MENTION_RE.finditer(text):
        raw = match.group(1)
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        handles.append(raw)
    return handles


async def resolve_mention_recipients(
    db: AsyncSession,
    handles: Iterable[str],
    *,
    exclude: set[uuid.UUID] | None = None,
) -> list[User]:
    """Resuelve handles a usuarios existentes (handle no nulo). Filtra por
    ``exclude`` (típicamente: el actor + cualquier usuario que ya recibe otra
    notif por este mismo evento)."""
    handle_list = [h.lower() for h in handles]
    if not handle_list:
        return []
    skip = exclude or set()
    result = await db.execute(
        select(User).where(User.handle.in_(handle_list))
    )
    return [u for u in result.scalars().all() if u.id not in skip]
