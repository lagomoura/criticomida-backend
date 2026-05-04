"""Unit tests para `validate_price_paid` (capa 1) y para la lógica pura del
detector de outlier (capa 2). El detector hace I/O contra la BD; acá lo que
testeamos por unit es la matemática (mediana, ratio, mínimo histórico) usando
una sesión mockeada que devuelve la lista de precios. El smoke real
end-to-end queda en los integration tests del timeline.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.services.price_validation import (
    FLAG_OUTLIER_HIGH,
    FLAG_OUTLIER_LOW,
    evaluate_price_outlier,
    validate_price_paid,
)


def test_none_is_accepted():
    """Campo opcional → None pasa sin error."""
    validate_price_paid(None, "ARS")
    validate_price_paid(None, None)


def test_zero_or_negative_raises_422():
    with pytest.raises(HTTPException) as exc:
        validate_price_paid(Decimal("0"), "ARS")
    assert exc.value.status_code == 422
    assert "greater than 0" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        validate_price_paid(Decimal("-100"), "ARS")
    assert exc.value.status_code == 422


def test_ars_in_range_passes():
    # Cap ARS: 50..5_000_000
    validate_price_paid(Decimal("50"), "ARS")  # mínimo inclusive
    validate_price_paid(Decimal("4500"), "ARS")  # caso típico hoy
    validate_price_paid(Decimal("5000000"), "ARS")  # máximo inclusive


def test_ars_below_min_raises():
    with pytest.raises(HTTPException) as exc:
        validate_price_paid(Decimal("10"), "ARS")
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["error"] == "price_paid_out_of_range"
    assert detail["currency"] == "ARS"


def test_ars_above_max_raises():
    with pytest.raises(HTTPException) as exc:
        validate_price_paid(Decimal("999999999"), "ARS")
    assert exc.value.status_code == 422
    assert exc.value.detail["error"] == "price_paid_out_of_range"


def test_usd_caps_are_distinct():
    # USD: 1..2000
    validate_price_paid(Decimal("1"), "USD")
    validate_price_paid(Decimal("45"), "USD")
    validate_price_paid(Decimal("2000"), "USD")
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("0.5"), "USD")
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("5000"), "USD")


def test_brl_caps_are_distinct():
    # BRL: 5..5000
    validate_price_paid(Decimal("12.50"), "BRL")
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("3"), "BRL")
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("10000"), "BRL")


def test_currency_is_normalized_to_uppercase():
    # El frontend podría mandar "ars" en lower case por error del usuario;
    # lo aceptamos y lo tratamos como ARS.
    validate_price_paid(Decimal("4500"), "ars")
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("10"), "ars")


def test_unknown_currency_uses_fallback():
    # Fallback: 0.01 .. 1_000_000_000. Acepta casi cualquier número positivo
    # razonable y solo rechaza el absurdo.
    validate_price_paid(Decimal("4500"), "XYZ")
    validate_price_paid(Decimal("0.01"), "XYZ")
    validate_price_paid(Decimal("1000000000"), "XYZ")


def test_unknown_currency_above_fallback_max_raises():
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("9999999999"), "XYZ")


def test_null_currency_uses_fallback():
    # Restaurante sin currency_code seteada → fallback.
    validate_price_paid(Decimal("4500"), None)
    with pytest.raises(HTTPException):
        validate_price_paid(Decimal("9999999999"), None)


# --- Capa 2: detector de outlier ---


def _mock_db_with_prices(prices: list[Decimal]) -> Any:
    """Mock mínimo de AsyncSession: el servicio solo llama
    ``db.execute(stmt)`` y luego ``.scalars().all()``. Devolvemos directo la
    lista de precios."""
    scalars = MagicMock()
    scalars.all.return_value = prices

    result = MagicMock()
    result.scalars.return_value = scalars

    db = MagicMock()

    async def execute(_stmt):  # noqa: ARG001 — el mock ignora el statement
        return result

    db.execute = execute
    return db


@pytest.mark.asyncio
async def test_outlier_returns_none_when_price_is_none():
    db = _mock_db_with_prices([Decimal("4500"), Decimal("5000"), Decimal("4800")])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=None
    )
    assert flagged_at is None and reason is None


@pytest.mark.asyncio
async def test_outlier_returns_none_when_history_under_min():
    # Solo 2 reviews previas → debajo de _MIN_HISTORY=3, no flagueamos.
    db = _mock_db_with_prices([Decimal("5000"), Decimal("5200")])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("99999"),
    )
    assert flagged_at is None and reason is None


@pytest.mark.asyncio
async def test_outlier_high_when_price_exceeds_3x_median():
    # Mediana = 5000. 99999 > 3 × 5000 = 15000 → outlier_high.
    db = _mock_db_with_prices([Decimal("4500"), Decimal("5000"), Decimal("5500")])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("99999"),
    )
    assert flagged_at is not None
    assert reason == FLAG_OUTLIER_HIGH


@pytest.mark.asyncio
async def test_outlier_low_when_price_below_third_of_median():
    # Mediana = 5000. 1000 < 5000/3 ≈ 1666 → outlier_low.
    db = _mock_db_with_prices([Decimal("4500"), Decimal("5000"), Decimal("5500")])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("1000"),
    )
    assert flagged_at is not None
    assert reason == FLAG_OUTLIER_LOW


@pytest.mark.asyncio
async def test_outlier_within_range_does_not_flag():
    # Mediana = 5000. 8000 está adentro de [5000/3, 5000×3] = [1667, 15000].
    db = _mock_db_with_prices([Decimal("4500"), Decimal("5000"), Decimal("5500")])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("8000"),
    )
    assert flagged_at is None and reason is None


@pytest.mark.asyncio
async def test_outlier_at_exactly_3x_median_does_not_flag():
    # Borde inferior del flag: 15000 == 5000×3. Solo > supera el umbral.
    db = _mock_db_with_prices([Decimal("4500"), Decimal("5000"), Decimal("5500")])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("15000"),
    )
    assert flagged_at is None and reason is None


@pytest.mark.asyncio
async def test_outlier_with_even_count_uses_average_of_two_middles():
    # 4 valores → mediana = (5000+6000)/2 = 5500. 17000 > 5500 × 3 = 16500.
    db = _mock_db_with_prices(
        [Decimal("4500"), Decimal("5000"), Decimal("6000"), Decimal("7000")]
    )
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("17000"),
    )
    assert flagged_at is not None
    assert reason == FLAG_OUTLIER_HIGH


@pytest.mark.asyncio
async def test_self_delta_flags_when_edit_jumps_above_3x_previous():
    """Update path: el detector ahora compara el precio nuevo contra el
    anterior de la misma review. 5000 → 99999 = 20× → flag inmediato, sin
    necesidad de histórico del plato."""
    db = _mock_db_with_prices([])  # Sin histórico — el self-delta dispara igual.
    flagged_at, reason = await evaluate_price_outlier(
        db,
        dish_id=uuid.uuid4(),
        price_paid=Decimal("99999"),
        previous_price=Decimal("5000"),
    )
    assert flagged_at is not None
    assert reason == FLAG_OUTLIER_HIGH


@pytest.mark.asyncio
async def test_self_delta_flags_low_when_edit_drops_below_third():
    db = _mock_db_with_prices([])
    flagged_at, reason = await evaluate_price_outlier(
        db,
        dish_id=uuid.uuid4(),
        price_paid=Decimal("500"),
        previous_price=Decimal("5000"),
    )
    assert flagged_at is not None
    assert reason == FLAG_OUTLIER_LOW


@pytest.mark.asyncio
async def test_self_delta_does_not_flag_within_3x_range():
    # 5000 → 8000 = 1.6× → no flag.
    db = _mock_db_with_prices([])
    flagged_at, reason = await evaluate_price_outlier(
        db,
        dish_id=uuid.uuid4(),
        price_paid=Decimal("8000"),
        previous_price=Decimal("5000"),
    )
    assert flagged_at is None and reason is None


@pytest.mark.asyncio
async def test_self_delta_only_applies_when_previous_is_set():
    # Sin `previous_price` (create flow) y sin histórico → no flag.
    db = _mock_db_with_prices([])
    flagged_at, reason = await evaluate_price_outlier(
        db, dish_id=uuid.uuid4(), price_paid=Decimal("99999"),
    )
    assert flagged_at is None and reason is None
