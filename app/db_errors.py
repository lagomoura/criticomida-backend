"""PostgreSQL error codes for IntegrityError handling."""

UNIQUE_VIOLATION = '23505'


def is_unique_violation(error: BaseException) -> bool:
    """Return True if the exception is a Postgres unique constraint violation."""
    orig = getattr(error, 'orig', error)
    code = getattr(orig, 'pgcode', None)
    return code == UNIQUE_VIOLATION
