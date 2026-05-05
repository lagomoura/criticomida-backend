"""Unit tests for ``_check_numeric_groundedness`` in the eval runner.

The validator is the eval suite's catch for the failure mode that
breaks owner trust the hardest: the agent quoting a metric (rating,
response rate, review count) that no tool ever returned. The runner's
``tools_called`` assertion alone can't catch this — it sees the right
tool fired but doesn't compare what came back to what came out.

Tests pin three properties:

1. **Positive groundings pass.** The number is in tool output (exact
   or within tolerance) → no failure.
2. **Fabrications fail.** The number isn't in any tool output → a
   ``unverified_number`` failure with the offending literal.
3. **Noise filters hold.** Small integers, years, and explicitly
   allowlisted values don't trigger the check — otherwise every
   case lights up false positives.
"""

from __future__ import annotations

from tests.chat.evals.runner import (
    CapturedToolCall,
    _check_numeric_groundedness,
)


def _tool(output: object, name: str = "list_reviews") -> CapturedToolCall:
    return CapturedToolCall(
        name=name, args={}, output=output, is_error=False
    )


# ─────────────────────────────────────────────────────────────────
#   Positive — numbers are grounded
# ─────────────────────────────────────────────────────────────────


def test_passes_when_decimal_appears_in_tool_output() -> None:
    failures = _check_numeric_groundedness(
        final_text="Tu rating promedio es 4.2 sobre 5 estrellas.",
        tool_calls=[_tool({"rating_avg": 4.2, "count": 17})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_passes_when_percent_in_text_matches_fraction_in_output() -> None:
    failures = _check_numeric_groundedness(
        final_text="Tu tasa de respuesta es 85%.",
        tool_calls=[_tool({"response_rate": 0.85})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_passes_within_tolerance() -> None:
    failures = _check_numeric_groundedness(
        final_text="Promedio de 4.2.",
        tool_calls=[_tool({"avg": 4.18})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_passes_when_number_is_explicitly_allowed() -> None:
    failures = _check_numeric_groundedness(
        final_text="En los últimos 30 días recibiste 12 reseñas.",
        tool_calls=[_tool({"review_count": 12})],
        tolerance=0.05,
        allowed=[30.0],
    )
    assert failures == []


def test_handles_es_decimal_with_comma() -> None:
    failures = _check_numeric_groundedness(
        final_text="Promedio 4,2 estrellas.",
        tool_calls=[_tool({"avg": 4.2})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_walks_nested_tool_output() -> None:
    failures = _check_numeric_groundedness(
        final_text="El plato top tiene rating 4.7.",
        tool_calls=[
            _tool({"items": [{"name": "Tacos", "rating": 4.7}]})
        ],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


# ─────────────────────────────────────────────────────────────────
#   Negative — fabrications fail
# ─────────────────────────────────────────────────────────────────


def test_fails_on_fabricated_decimal() -> None:
    failures = _check_numeric_groundedness(
        final_text="Tu rating promedio es 4.7.",
        tool_calls=[_tool({"avg": 4.2})],
        tolerance=0.05,
        allowed=[],
    )
    assert len(failures) == 1
    assert "unverified_number" in failures[0]
    assert "'4.7'" in failures[0]


def test_fails_on_fabricated_percentage() -> None:
    failures = _check_numeric_groundedness(
        final_text="Tu tasa de respuesta es 85%.",
        tool_calls=[_tool({"response_rate": 0.42})],
        tolerance=0.05,
        allowed=[],
    )
    assert len(failures) == 1
    assert "'85%'" in failures[0]


def test_reports_each_fabrication_separately() -> None:
    failures = _check_numeric_groundedness(
        final_text="Tu rating es 4.7 con 250 reseñas.",
        tool_calls=[_tool({"avg": 4.2, "count": 17})],
        tolerance=0.05,
        allowed=[],
    )
    assert len(failures) == 2


# ─────────────────────────────────────────────────────────────────
#   Noise filters
# ─────────────────────────────────────────────────────────────────


def test_skips_small_integers() -> None:
    failures = _check_numeric_groundedness(
        final_text="Te muestro 3 puntos: 1) calidad 2) servicio 3) precio.",
        tool_calls=[_tool({})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_skips_years() -> None:
    failures = _check_numeric_groundedness(
        final_text="Datos desde 2024 hasta 2026.",
        tool_calls=[_tool({})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_no_failures_with_empty_text() -> None:
    failures = _check_numeric_groundedness(
        final_text="",
        tool_calls=[_tool({})],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_skips_when_no_tools_but_only_noise_numbers() -> None:
    failures = _check_numeric_groundedness(
        final_text="Mostré 3 ítems.",
        tool_calls=[],
        tolerance=0.05,
        allowed=[],
    )
    assert failures == []


def test_fails_when_no_tools_were_called_but_text_quotes_metrics() -> None:
    failures = _check_numeric_groundedness(
        final_text="Tu rating es 4.7.",
        tool_calls=[],
        tolerance=0.05,
        allowed=[],
    )
    assert len(failures) == 1
