"""Unit tests for the deterministic preference-intent detector.

The detector is the first layer of the 3-layer defence against the LLM
dropping ``update_owner_preferences`` calls. The contract:

- A trigger phrase ("siempre", "from now on", "sempre", …) **and** a
  tone/language keyword must co-occur in the **same sentence** before
  any field fires. Triggers alone never count.
- Tone enum mirror: ``warm`` / ``professional`` / ``concise``.
  ``match_brand`` is intentionally not extractable (too ambiguous).
- Language enum mirror: ``es`` / ``en`` / ``pt``.

These tests pin the false-positive guarantee — the only way to break
them is to relax the same-sentence constraint or drop the trigger
requirement, both of which would let "siempre quise probarlo" or
"my flag is always set" leak through and corrupt the owner's prefs.
"""

from __future__ import annotations

import pytest

from app.services.chat.preference_intent import detect_preference_intent


# ─────────────────────────────────────────────────────────────────
#   Positive cases — explicit persistent intent in 3 languages
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message,expected_tone",
    [
        ("De ahora en más siempre respondé corto y al hueso.", "concise"),
        ("Siempre usá un tono más formal por favor.", "professional"),
        ("Por defecto quiero un tono cálido en cada respuesta.", "warm"),
        ("From now on always be concise.", "concise"),
        ("Always keep the tone professional, by default.", "professional"),
        ("Sempre responda de forma profissional.", "professional"),
        ("Por padrão usa um tom caloroso.", "warm"),
    ],
)
def test_detects_tone_intent(message: str, expected_tone: str) -> None:
    intent = detect_preference_intent(message)
    assert intent is not None
    assert intent.get("tone") == expected_tone


@pytest.mark.parametrize(
    "message,expected_lang",
    [
        ("De ahora en más respondé siempre en portugués.", "pt"),
        ("Sempre responde em português daqui em diante, por favor.", "pt"),
        ("From now on always reply in English.", "en"),
        ("Por defecto contestá en español.", "es"),
    ],
)
def test_detects_language_intent(
    message: str, expected_lang: str
) -> None:
    intent = detect_preference_intent(message)
    assert intent is not None
    assert intent.get("language") == expected_lang


def test_detects_tone_and_language_together() -> None:
    intent = detect_preference_intent(
        "Siempre respondé en inglés y con un tono formal."
    )
    assert intent == {"tone": "professional", "language": "en"}


# ─────────────────────────────────────────────────────────────────
#   Negative cases — the detector must NOT fire here
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        # Trigger phrase used in unrelated meaning.
        "Siempre quise probar un risotto de hongos.",
        "I always order the special.",
        "Sempre quis abrir um restaurante.",
        # Tone/language keyword without a trigger.
        "Respondé corto, por favor.",
        "Could you reply in English?",
        "Responda em português.",
        # One-off request explicitly limited to this turn.
        "Esta vez respondé bien corto.",
        # Empty / blank.
        "",
        "   ",
        # Trigger and keyword in different sentences.
        "Siempre llego tarde. Respondé corto.",
    ],
)
def test_does_not_fire_on_non_persistent_intents(message: str) -> None:
    assert detect_preference_intent(message) is None


def test_returns_none_when_only_kpi_focus_mentioned() -> None:
    # KPI focus is intentionally out of scope for this detector — owner
    # who wants to pin KPIs goes through the LLM tool or the settings
    # panel. We assert nothing fires so we don't accidentally invent a
    # KPI list out of free-form text.
    assert (
        detect_preference_intent(
            "De ahora en más siempre quiero ver el rating en el saludo."
        )
        is None
    )
