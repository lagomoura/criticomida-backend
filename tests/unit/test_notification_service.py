"""Unit tests for the like-notification text enrichment.

Cover the pure logic that turns a `_ReviewLikeContext` into a denormalized
notification text, including the pillar tiebreaker (execution > value_prop >
presentation, mirroring the Geek Score weights).
"""

from app.services.notification_service import (
    _ReviewLikeContext,
    _build_like_text,
    _top_pillar,
)


def _ctx(
    *,
    dish: str | None = "Milanesa Napolitana",
    presentation: int | None = None,
    value_prop: int | None = None,
    execution: int | None = None,
) -> _ReviewLikeContext:
    return _ReviewLikeContext(
        dish_name=dish,
        presentation=presentation,
        value_prop=value_prop,
        execution=execution,
    )


def test_build_text_falls_back_when_review_missing():
    text = _build_like_text(_ctx(dish=None))
    assert text == "le dio like a tu reseña."


def test_build_text_uses_dish_when_no_pillar_is_three():
    text = _build_like_text(_ctx(execution=2, value_prop=2, presentation=2))
    assert text == "le dio like a tu reseña de Milanesa Napolitana."


def test_build_text_highlights_execution_when_three():
    text = _build_like_text(_ctx(execution=3))
    assert "Ejecución" in text
    assert "👨‍🍳" in text
    assert "Milanesa Napolitana" in text


def test_build_text_highlights_value_prop_when_three():
    text = _build_like_text(_ctx(value_prop=3))
    assert "hallazgo" in text
    assert "💎" in text


def test_build_text_highlights_presentation_when_three():
    text = _build_like_text(_ctx(presentation=3))
    assert "Presentación" in text
    assert "🌟" in text


def test_top_pillar_breaks_tie_in_priority_order():
    # Los tres pilares en 3: gana execution (peso más alto del Geek Score).
    assert _top_pillar(_ctx(execution=3, value_prop=3, presentation=3)) == "execution"

    # Sin execution: gana value_prop sobre presentation.
    assert _top_pillar(_ctx(value_prop=3, presentation=3)) == "value_prop"

    # Solo presentation: queda presentation.
    assert _top_pillar(_ctx(presentation=3)) == "presentation"

    # Ningún pilar en 3: None (texto legacy).
    assert _top_pillar(_ctx(execution=2, value_prop=1)) is None


def test_top_pillar_ignores_lower_values():
    # value_prop=2 no califica aunque execution=3 esté ausente.
    assert _top_pillar(_ctx(value_prop=2, presentation=2)) is None
