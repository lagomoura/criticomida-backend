"""Validación de precio en reseñas — capa 1 (caps por moneda) y capa 2
(detector de outlier contra el histórico del plato).

Capa 1 — `validate_price_paid`: rechaza valores absurdos antes de tocar la BD.
Devuelve 422 con detail estructurado. Mantenimiento: revisar los rangos cada
6 meses, sobre todo ARS por inflación. Los caps son a propósito amplios — no
queremos rechazar al menú degustación de $300.000 en Buenos Aires; queremos
rechazar al typo de "999999999".

Capa 2 — `evaluate_price_outlier`: NO rechaza, soft-flagea. Compara contra la
mediana de las reviews previas con precio (no flagged) del mismo plato. Si la
desviación supera el ratio configurado (default 3×), devuelve un motivo
(`outlier_high` / `outlier_low`) que el caller persiste en
`price_flagged_at`/`price_flag_reason`. El timeline excluye los flagged del
avg hasta que un humano resuelva.

Por qué soft-flag y no rechazo: una review de un menú degustación legítimo
tiene contenido valioso (texto, rating, fotos) que el lector quiere ver. El
precio puede explicarse en el texto. La capa 3 (no implementada todavía)
notifica al owner y al admin para revisión humana.

Cuando el restaurante no tiene `currency_code` (NULL), capa 1 cae a un rango
fallback amplio. Capa 2 funciona igual — la moneda no afecta la mediana,
solo la magnitud absoluta del valor.
"""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# Rangos por ISO 4217. (min_inclusive, max_inclusive). Decimal para evitar
# imprecisión de float. Usar `_CAPS_FALLBACK` cuando la moneda es desconocida.
PRICE_CAPS: dict[str, tuple[Decimal, Decimal]] = {
    "ARS": (Decimal("50"), Decimal("5000000")),
    "BRL": (Decimal("5"), Decimal("5000")),
    "USD": (Decimal("1"), Decimal("2000")),
    "EUR": (Decimal("1"), Decimal("2000")),
    "CLP": (Decimal("500"), Decimal("500000")),
    "UYU": (Decimal("100"), Decimal("100000")),
    "COP": (Decimal("1000"), Decimal("5000000")),
    "MXN": (Decimal("20"), Decimal("50000")),
}

# Cuando no sabemos la moneda, aceptamos casi cualquier número positivo que
# entre en `Numeric(12,2)` pero seguimos cortando absurdos (>1B).
_CAPS_FALLBACK: tuple[Decimal, Decimal] = (Decimal("0.01"), Decimal("1000000000"))


def validate_price_paid(
    price_paid: Decimal | None,
    currency_code: str | None,
) -> None:
    """Valida que `price_paid` esté dentro del rango razonable para la moneda.

    No hace nada cuando `price_paid` es `None` (el campo es opcional). Tira
    `HTTPException 422` cuando el valor está fuera del rango — el frontend ya
    filtra `<= 0` y `NaN`, pero acá no asumimos eso porque el endpoint también
    se llama desde scripts y otros clients.
    """
    if price_paid is None:
        return

    if price_paid <= 0:
        # Defensa redundante con el CHECK de la migración 038, pero respondemos
        # 422 amistoso en lugar de un error opaco de DB.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="price_paid must be greater than 0",
        )

    code = (currency_code or "").upper()
    cap_min, cap_max = PRICE_CAPS.get(code, _CAPS_FALLBACK)

    if price_paid < cap_min or price_paid > cap_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "price_paid_out_of_range",
                "currency": code or None,
                "min": str(cap_min),
                "max": str(cap_max),
                "received": str(price_paid),
            },
        )


# --- Capa 2: detector de outlier por mediana ---

# Hace falta este mínimo de reviews previas con precio (no flagged) para que
# el detector se active. Por debajo de eso la mediana es ruido y prefiero
# dejar pasar el precio que castigar a los primeros cronistas del plato.
_MIN_HISTORY = 3

# Umbral de desviación: precio > mediana × ratio → outlier_high.
# precio < mediana / ratio → outlier_low. 3× es conservador: tolera el menú
# degustación legítimo (que es 5–10× la entrada cuando el restaurante mezcla
# carta + experiencia degustación) pero atrapa el typo de "extra cero".
_OUTLIER_RATIO = Decimal("3")


# Razones canónicas. Acompañan al timestamp en `price_flag_reason`.
FLAG_OUTLIER_HIGH = "outlier_high"
FLAG_OUTLIER_LOW = "outlier_low"


async def evaluate_price_outlier(
    db: AsyncSession,
    *,
    dish_id: uuid.UUID,
    price_paid: Decimal | None,
    exclude_review_id: uuid.UUID | None = None,
    previous_price: Decimal | None = None,
) -> tuple[datetime | None, str | None]:
    """Decide si `price_paid` es un outlier para este plato.

    Devuelve `(flagged_at, reason)` listo para volcar en las columnas de la
    review. Cuando no hay flag, devuelve `(None, None)`.

    Combina dos signals:

    1. **Self-delta** (solo aplica cuando `previous_price` está seteado, o sea
       en el flow de update). Si el precio nuevo difiere del anterior de la
       misma review por más de `_OUTLIER_RATIO`×, flag inmediato. Catchea
       ediciones donde el crítico sube/baja drásticamente su propio número
       — un patrón típico de fraude / typo posterior. No requiere historial.

    2. **Mediana del histórico del plato**. Solo se activa con
       ≥ `_MIN_HISTORY` reviews previas con precio y sin flag. Útil para
       reviews nuevas: si se aparta >3× de la mediana del plato, flag.

    `exclude_review_id` evita comparar la review contra sí misma cuando
    consultamos el histórico (autoflush podría haber escrito ya el valor
    nuevo). Solo cuenta reviews con `price_flagged_at IS NULL`: un troll con
    muchas reseñas flagged no puede establecer una mediana nueva.
    """
    if price_paid is None or price_paid <= 0:
        return None, None

    # 1) Self-delta — barato y no necesita query a la BD.
    if previous_price is not None and previous_price > 0:
        if price_paid > previous_price * _OUTLIER_RATIO:
            return datetime.now(timezone.utc), FLAG_OUTLIER_HIGH
        if price_paid < previous_price / _OUTLIER_RATIO:
            return datetime.now(timezone.utc), FLAG_OUTLIER_LOW

    # 2) Mediana del histórico. Importación local para evitar ciclo de imports.
    from app.models.dish import DishReview

    stmt = select(DishReview.price_paid).where(
        DishReview.dish_id == dish_id,
        DishReview.price_paid.is_not(None),
        DishReview.price_flagged_at.is_(None),
    )
    if exclude_review_id is not None:
        stmt = stmt.where(DishReview.id != exclude_review_id)

    rows = (await db.execute(stmt)).scalars().all()
    if len(rows) < _MIN_HISTORY:
        return None, None

    median = statistics.median(Decimal(str(v)) for v in rows)
    if median <= 0:
        # Defensivo: no debería pasar (capa 1 + CHECK lo garantizan), pero
        # evita ZeroDivision en el ratio.
        return None, None

    if price_paid > median * _OUTLIER_RATIO:
        return datetime.now(timezone.utc), FLAG_OUTLIER_HIGH
    if price_paid < median / _OUTLIER_RATIO:
        return datetime.now(timezone.utc), FLAG_OUTLIER_LOW
    return None, None
