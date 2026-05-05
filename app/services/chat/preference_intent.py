"""Deterministic detector for owner preference intents in the chat.

When the owner explicitly asks for a *persistent* change ("siempre tono
formal", "from now on respond in English"), the LLM sometimes confirms
verbally without calling the ``update_owner_preferences`` tool — so the
preference never lands in the DB.

This module is the **first layer** of a 3-layer defence (see
``docs/chatbot.md`` and the ``feedback_resolve_entidades`` memory):

1. **This regex preprocessor** — runs in ``chat_service.stream_chat``
   before the LLM. If it detects an explicit persistent intent it
   writes the preference straight to the DB and tells the LLM (via a
   transient system note) that the save already happened.
2. **The ``update_owner_preferences`` tool** — still callable by the
   LLM. Idempotent re-write on the same values is harmless.
3. **Audit script with a rate threshold** — catches drift in prod when
   neither the regex nor the LLM picked up the intent.

False-positive avoidance is the main concern ("siempre quise probarlo"
should *not* fire). A trigger phrase alone is never enough — we require
co-occurrence with a tone or language keyword **in the same sentence**.
"""

from __future__ import annotations

import re
from typing import TypedDict


class PreferenceIntent(TypedDict, total=False):
    """Subset of fields that the regex preprocessor can extract.

    Mirrors a subset of ``UpdateOwnerPreferencesInput``. We deliberately
    skip ``kpi_focus`` here: KPI names are open-ended strings and the
    risk of mis-extraction outweighs the benefit. Owners who want to
    pin KPIs go through the LLM tool or (eventually) the settings page.
    """

    tone: str
    language: str


# ──────────────────────────────────────────────────────────────────────
#   Trigger phrases — must co-occur with a tone/language keyword in the
#   same sentence to avoid matching innocuous uses of "siempre", etc.
# ──────────────────────────────────────────────────────────────────────
_TRIGGER_PATTERNS = [
    # Spanish
    r"\bsiempre\b",
    r"\bde ahora en m[áa]s\b",
    r"\bde aqu[íi] en adelante\b",
    r"\bpor defecto\b",
    # Portuguese
    r"\bsempre\b",
    r"\bde agora em diante\b",
    r"\bdaqui em diante\b",
    r"\bpor padr[ãa]o\b",
    # English
    r"\bfrom now on\b",
    r"\balways\b",
    r"\bby default\b",
]
_TRIGGER_RE = re.compile("|".join(_TRIGGER_PATTERNS), re.IGNORECASE)


# Tone keywords map to ``OwnerPreferenceTone`` enum values. The
# ``match_brand`` value is intentionally absent — phrases like "como
# nuestra marca" are too ambiguous to extract deterministically.
_TONE_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(formal|profesional|professional|profissional)\b", re.IGNORECASE
        ),
        "professional",
    ),
    (
        re.compile(
            r"\b(c[áa]lido|warm|cercan[oa]|caloroso|amig[áa]vel|amigable)\b",
            re.IGNORECASE,
        ),
        "warm",
    ),
    (
        re.compile(
            r"\b(corto|conciso|concis[oa]|breve|concise|short|curto|"
            r"al hueso|directo)\b",
            re.IGNORECASE,
        ),
        "concise",
    ),
]


# Language keywords map to ``OwnerPreferenceLanguage`` enum values.
# The pattern requires a preposition ("en", "in", "em") so that bare
# language names inside dish descriptions etc. don't trigger.
_LANGUAGE_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(en\s+espa[ñn]ol|en\s+castellano|in\s+spanish|em\s+espanhol)\b",
            re.IGNORECASE,
        ),
        "es",
    ),
    (
        re.compile(
            r"\b(en\s+ingl[ée]s|in\s+english|em\s+ingl[êe]s)\b",
            re.IGNORECASE,
        ),
        "en",
    ),
    (
        re.compile(
            r"\b(en\s+portugu[ée]s|in\s+portuguese|em\s+portugu[êe]s)\b",
            re.IGNORECASE,
        ),
        "pt",
    ),
]


_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def detect_preference_intent(message: str) -> PreferenceIntent | None:
    """Return persistent prefs explicitly requested in ``message``.

    Returns ``None`` when no explicit intent was found. The detector is
    intentionally conservative: a trigger phrase ("always", "siempre",
    "from now on", …) and a tone or language keyword must both appear
    in the **same sentence** before any field fires.

    The first matching value per field wins — if a sentence somehow
    mentions two tones, we take the leftmost in the keyword table.
    """
    if not message:
        return None

    intent: PreferenceIntent = {}
    for sentence in _SENTENCE_SPLIT_RE.split(message):
        if not _TRIGGER_RE.search(sentence):
            continue

        if "tone" not in intent:
            for pattern, value in _TONE_KEYWORDS:
                if pattern.search(sentence):
                    intent["tone"] = value
                    break

        if "language" not in intent:
            for pattern, value in _LANGUAGE_KEYWORDS:
                if pattern.search(sentence):
                    intent["language"] = value
                    break

    return intent or None
