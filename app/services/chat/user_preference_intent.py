"""Deterministic detector for **comensal** preference intents in the chat.

B2C mirror of ``preference_intent`` (Business). When the comensal
explicitly asks for a *persistent* change ("siempre respondé en
inglés", "from now on hablame corto"), the LLM sometimes confirms
verbally without calling ``update_user_chat_preferences`` — the
preference never lands in the DB.

Same 3-layer defence pattern as the Business side:

1. **This regex preprocessor** — runs in ``chat_service.stream_chat``
   before the LLM. If it detects an explicit persistent intent it
   writes the preference and tells the LLM (transient system note)
   that the save already happened, so the model just confirms.
2. **The ``update_user_chat_preferences`` tool** — still callable by
   the LLM. Idempotent re-write on the same values is harmless.
3. **Audit script with a rate threshold** — already supports
   ``--agent sommelier`` (see ``audit_chat_handoffs.py``).

False-positive avoidance is the main concern ("siempre quise
probarlo" should *not* fire). A trigger phrase alone is never
enough — we require co-occurrence with a language or response-style
keyword **in the same sentence**.
"""

from __future__ import annotations

import re
from typing import TypedDict


class UserPreferenceIntent(TypedDict, total=False):
    """Subset of fields the regex can extract for the comensal."""

    language: str
    response_style: str


# ──────────────────────────────────────────────────────────────────────
#   Trigger phrases — must co-occur with a language/style keyword in
#   the same sentence to avoid matching innocuous uses of "siempre".
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


# Language keywords map to ``UserPreferenceLanguage`` enum values.
# Same shape as the Business side — the preposition requirement
# avoids matching bare language names inside dish descriptions.
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


# Response-style keywords map to ``UserResponseStyle`` enum values.
#
# ``concise`` is the most-asked-for style ("hablame corto", "no me
# escribas tanto", "andá al grano") so it gets the broadest
# vocabulary. ``editorial`` is intentionally narrow — most users
# don't ask for it by name; it's the default.
_STYLE_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(corto|conciso|concis[oa]|breve|concise|short|curto|"
            r"al hueso|al grano|directo|sin rodeos)\b",
            re.IGNORECASE,
        ),
        "concise",
    ),
    (
        re.compile(
            r"\b(c[áa]lid[oa]|warm|cercan[oa]|caloros[oa]|"
            r"conversacional|conversational|amig[áa]vel|amigable)\b",
            re.IGNORECASE,
        ),
        "warm",
    ),
    (
        re.compile(
            r"\b(editorial|narrativo|contextualizado|"
            r"editorial style)\b",
            re.IGNORECASE,
        ),
        "editorial",
    ),
]


_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def detect_user_preference_intent(
    message: str,
) -> UserPreferenceIntent | None:
    """Return persistent prefs explicitly requested in ``message``.

    Returns ``None`` when no explicit intent was found. Conservative
    by design: a trigger phrase ("siempre", "always", "from now on",
    …) and a language or response-style keyword must both appear in
    the **same sentence** before any field fires.

    The first matching value per field wins — if a sentence somehow
    mentions two styles, we take the leftmost in the keyword table.
    """
    if not message:
        return None

    intent: UserPreferenceIntent = {}
    for sentence in _SENTENCE_SPLIT_RE.split(message):
        if not _TRIGGER_RE.search(sentence):
            continue

        if "language" not in intent:
            for pattern, value in _LANGUAGE_KEYWORDS:
                if pattern.search(sentence):
                    intent["language"] = value
                    break

        if "response_style" not in intent:
            for pattern, value in _STYLE_KEYWORDS:
                if pattern.search(sentence):
                    intent["response_style"] = value
                    break

    return intent or None
