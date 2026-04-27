"""Detect "schema exists but alembic_version is empty" and signal a stamp.

Background: a `pg_dump --data-only` reseed of an existing DB can wipe the
`alembic_version` row while leaving every other table (and PG enum) intact.
On the next `alembic upgrade head`, alembic re-runs migration 001 from
scratch and crashes on `CREATE TYPE user_role`. This script is the guard
the entrypoint runs *before* `upgrade head`: when it exits 0 the entrypoint
runs `alembic stamp head`, when it exits 1 nothing happens.

Heuristic: a fresh DB has no `users` table and `upgrade head` is correct.
A populated DB *with* a tracked alembic_version row is also fine. Only the
half-tracked state — `users` exists, `alembic_version` is empty/missing —
needs a stamp.
"""
import asyncio
import os
import sys

import asyncpg


def _libpq_url(url: str) -> str:
    """Strip SQLAlchemy driver prefix; asyncpg uses libpq-style URLs."""
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
    ):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


async def needs_stamp() -> bool:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        return False
    conn = await asyncpg.connect(_libpq_url(raw))
    try:
        users = await conn.fetchval("SELECT to_regclass('public.users')")
        if users is None:
            return False
        version_table = await conn.fetchval(
            "SELECT to_regclass('public.alembic_version')"
        )
        if version_table is None:
            return True
        current = await conn.fetchval(
            "SELECT version_num FROM alembic_version LIMIT 1"
        )
        return current is None
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(needs_stamp()) else 1)
