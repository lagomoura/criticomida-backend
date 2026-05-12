"""Carga muestras del estilo de escritura del usuario.

Hoy lo consume el Ghostwriter para que el ``editorial_blurb`` imite la
voz del autor (registro, longitud de frase, vocabulario, muletillas) en
lugar de sonar al tono editorial neutro de Palato.

Sólo devuelve notas no triviales (``STYLE_SAMPLE_MIN_LEN`` chars) y
clipea cada una para no inflar el prompt cuando el usuario es prolífico.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import DishReview


STYLE_SAMPLES_LIMIT = 5
STYLE_SAMPLE_CHAR_CAP = 500
STYLE_SAMPLE_MIN_LEN = 30


async def fetch_style_samples(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    exclude_dish_id: uuid.UUID | None = None,
) -> list[str]:
    """Las últimas ``STYLE_SAMPLES_LIMIT`` notas del usuario, más reciente
    primero. Filtra notas demasiado cortas para que sean señal de voz y,
    si se pasa ``exclude_dish_id``, descarta reseñas del mismo plato para
    que un re-review no se auto-cite.
    """
    stmt = (
        select(DishReview.note)
        .where(DishReview.user_id == user_id)
        .where(func.char_length(DishReview.note) >= STYLE_SAMPLE_MIN_LEN)
        .order_by(DishReview.created_at.desc())
        .limit(STYLE_SAMPLES_LIMIT)
    )
    if exclude_dish_id is not None:
        stmt = stmt.where(DishReview.dish_id != exclude_dish_id)

    notes = list((await db.execute(stmt)).scalars().all())
    return [n.strip()[:STYLE_SAMPLE_CHAR_CAP] for n in notes if n and n.strip()]
