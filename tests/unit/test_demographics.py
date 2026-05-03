"""Unit tests for ``derive_age_range``.

Verifies bucket boundaries (17/18, 24/25, 64/65, 65+) and that a
birthday that hasn't happened yet this year still returns the lower
age (i.e. someone born 1990-12-31 is 34 on 2025-06-15, not 35).
"""

from __future__ import annotations

from datetime import date

from app.services.demographics import derive_age_range


REF = date(2026, 5, 3)


def test_returns_none_when_birth_is_none():
    assert derive_age_range(None, today=REF) is None


def test_under_18_bucket():
    # Turns 17 on 2026-05-03 → still <18
    assert derive_age_range(date(2009, 5, 3), today=REF) == "<18"


def test_exactly_18_falls_into_next_bucket():
    assert derive_age_range(date(2008, 5, 3), today=REF) == "18-24"


def test_24_25_boundary():
    assert derive_age_range(date(2001, 5, 4), today=REF) == "18-24"
    assert derive_age_range(date(2001, 5, 3), today=REF) == "25-34"


def test_64_65_boundary():
    assert derive_age_range(date(1961, 5, 4), today=REF) == "55-64"
    assert derive_age_range(date(1961, 5, 3), today=REF) == "65+"


def test_centenarian_in_top_bucket():
    assert derive_age_range(date(1925, 1, 1), today=REF) == "65+"


def test_birthday_not_yet_passed_this_year():
    # Born 1990-12-31, today 2026-05-03 → still 35 (not 36)
    assert derive_age_range(date(1990, 12, 31), today=REF) == "35-44"
