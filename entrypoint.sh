#!/bin/sh
# Backend entrypoint shared by Docker (dev) and Railway (prod).
# Runs DB migrations, then starts uvicorn. If migrations fail, the container
# does not start — Railway/Docker will surface the error in logs.

set -e

echo "[entrypoint] Running alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn on port ${PORT:-8000}"
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --workers "${UVICORN_WORKERS:-2}"
