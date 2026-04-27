#!/bin/sh
# Backend entrypoint shared by Docker (dev) and Railway (prod).
# Runs DB migrations, then starts uvicorn. If migrations fail, the container
# does not start — Railway/Docker will surface the error in logs.

set -e

# Guard: a `pg_dump --data-only` reseed can leave the schema intact but wipe
# the alembic_version row, which makes `upgrade head` retry 001 and crash on
# duplicate enums. Detect that state and stamp head before upgrading.
if python scripts/safe_migrate.py; then
    echo "[entrypoint] Schema exists without alembic_version — stamping head"
    alembic stamp head
fi

echo "[entrypoint] Running alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn on port ${PORT:-8000}"
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${UVICORN_WORKERS:-2}"
