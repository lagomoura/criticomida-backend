#!/bin/sh
# Backend entrypoint shared by Docker (dev) and Railway (prod).
# Runs DB migrations, then starts uvicorn. If migrations fail, the container
# does not start — Railway/Docker will surface the error in logs.

set -e

echo "[entrypoint] Running alembic upgrade head..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn on port ${PORT:-8000} (APP_ENV=${APP_ENV:-development})"
if [ "${APP_ENV:-development}" = "production" ]; then
  exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${UVICORN_WORKERS:-2}"
else
  # Dev: hot-reload on source changes. Incompatible with --workers, so we
  # drop it here. Volume mount in docker-compose.yml feeds the watcher.
  exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --reload
fi
