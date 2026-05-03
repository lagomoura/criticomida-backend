"""Pure helpers for user demographics that the API exposes downstream.

The owner dashboard never sees a raw ``birth_date`` — it only sees the
bucketed ``age_range`` returned by ``derive_age_range``. Keeping this
mapping in one place ensures every endpoint that exposes age uses the
same buckets.
"""

from __future__ import annotations

from datetime import date


_BUCKETS: tuple[tuple[int, str], ...] = (
    (18, "<18"),
    (25, "18-24"),
    (35, "25-34"),
    (45, "35-44"),
    (55, "45-54"),
    (65, "55-64"),
)


def derive_age_range(birth: date | None, *, today: date | None = None) -> str | None:
    """Map an exact birth date to a privacy-preserving age bucket.

    Returns ``None`` when ``birth`` is ``None`` so callers can pass the
    column straight through. The optional ``today`` parameter exists
    only to make the function deterministic in tests.
    """
    if birth is None:
        return None
    if today is None:
        today = date.today()
    age = today.year - birth.year - (
        (today.month, today.day) < (birth.month, birth.day)
    )
    for upper, label in _BUCKETS:
        if age < upper:
            return label
    return "65+"
