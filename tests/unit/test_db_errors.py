"""Unit tests for Postgres error helpers."""

from app.db_errors import is_unique_violation


class _Orig:
    def __init__(self, pgcode: str | None) -> None:
        self.pgcode = pgcode


class _IntegrityError(Exception):
    """Minimal stand-in for sqlalchemy.exc.IntegrityError."""

    def __init__(self, pgcode: str | None) -> None:
        super().__init__("constraint")
        self.orig = _Orig(pgcode)


def test_is_unique_violation_true_for_sqlstate_23505():
    error = _IntegrityError("23505")

    assert is_unique_violation(error) is True


def test_is_unique_violation_false_for_other_sqlstate():
    error = _IntegrityError("23503")

    assert is_unique_violation(error) is False


def test_is_unique_violation_uses_orig_when_no_pgcode_on_wrapper():
    class _Bare(Exception):
        pass

    bare = _Bare("fail")
    bare.orig = _Orig("23505")

    assert is_unique_violation(bare) is True
