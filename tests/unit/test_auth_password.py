"""Unit tests for password hashing helpers."""

from app.middleware.auth import hash_password, verify_password


def test_verify_password_accepts_round_trip_hash():
    plain = "a-secure-passphrase-1"
    hashed = hash_password(plain)

    assert verify_password(plain, hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")

    assert verify_password("wrong-password", hashed) is False
