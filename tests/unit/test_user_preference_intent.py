"""Unit tests for the deterministic B2C preference-intent detector.

Mirror of ``test_preference_intent.py`` (Business). The detector is
the first layer of the 3-layer defence against the LLM dropping
``update_user_chat_preferences`` calls. Same contract:

- A trigger phrase ("siempre", "from now on", "sempre", …) **and** a
  language or response-style keyword must co-occur in the **same
  sentence** before any field fires. Triggers alone never count.
- Language enum mirror: ``es`` / ``en`` / ``pt``.
- Response-style enum mirror: ``editorial`` / ``concise`` / ``warm``.

These tests pin the false-positive guarantee — "siempre quise
probarlo" does NOT fire even though it contains the trigger
"siempre".
"""

from __future__ import annotations

import pytest

from app.services.chat.user_preference_intent import (
    detect_user_preference_intent,
)


# ─────────────────────────────────────────────────────────────────
#   Positive cases — explicit persistent intent, three languages
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected_style",
    [
        ("De ahora en más siempre respondé corto, al hueso.", "concise"),
        ("Siempre andá al grano por favor.", "concise"),
        ("Por defecto quiero que me hables más cálido.", "warm"),
        ("From now on always be concise.", "concise"),
        ("Always keep it warm and conversational.", "warm"),
        ("Sempre responda de forma concisa.", "concise"),
        ("Por padrão fale de forma calorosa.", "warm"),
    ],
)
def test_detects_response_style_intent(
    message: str, expected_style: str
) -> None:
    intent = detect_user_preference_intent(message)
    assert intent is not None
    assert intent.get("response_style") == expected_style


@pytest.mark.parametrize(
    "message,expected_lang",
    [
        ("De ahora en más respondeme siempre en inglés.", "en"),
        ("Por defecto en español, por favor.", "es"),
        ("From now on always reply in Portuguese.", "pt"),
        ("Sempre em português, por favor.", "pt"),
    ],
)
def test_detects_language_intent(message: str, expected_lang: str) -> None:
    intent = detect_user_preference_intent(message)
    assert intent is not None
    assert intent.get("language") == expected_lang


def test_detects_both_in_one_sentence() -> None:
    intent = detect_user_preference_intent(
        "De ahora en más respondeme siempre en inglés y al grano."
    )
    assert intent == {"language": "en", "response_style": "concise"}


# ─────────────────────────────────────────────────────────────────
#   Negative cases — trigger alone is NOT enough
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        # Trigger alone, no style/language keyword.
        "Siempre quise probar ese restaurante.",
        "Siempre voy a Palermo para comer.",
        "From now on I want to start saving more dishes.",
        "Sempre gostei de ramen.",
        # Style/language without a trigger.
        "Hablame corto en este turno, por favor.",
        "Just be concise.",
        # Empty / whitespace.
        "",
        "   ",
    ],
)
def test_no_false_positives(message: str) -> None:
    assert detect_user_preference_intent(message) is None


def test_trigger_and_keyword_in_different_sentences_does_not_fire() -> None:
    # Same-sentence requirement: split by .!? — these two clauses are
    # in separate sentences so the regex must NOT fire.
    assert (
        detect_user_preference_intent(
            "Siempre voy a Palermo. Hablame conciso esta vez."
        )
        is None
    )
