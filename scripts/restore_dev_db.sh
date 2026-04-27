#!/bin/sh
# Restore the local Docker dev database from a pg_dump snapshot taken from
# Railway prod. The snapshot file is gitignored — get a fresh copy with:
#
#   pg_dump --no-owner --no-acl --format=custom \
#     "<RAILWAY_PUBLIC_DATABASE_URL>" \
#     > backend/scripts/seeds/dev_baseline.dump
#
# Usage (from backend/):
#   docker compose up -d db
#   ./scripts/restore_dev_db.sh
#   docker compose up api
#
# Override the dump path with: DUMP=path/to/other.dump ./scripts/restore_dev_db.sh

set -e

DUMP="${DUMP:-scripts/seeds/dev_baseline.dump}"
PG_USER="${POSTGRES_USER:-criticomida}"
PG_DB="${POSTGRES_DB:-criticomida}"

if [ ! -f "$DUMP" ]; then
  echo "Dump not found at $DUMP"
  echo "Take one from Railway with pg_dump (see header of this script)."
  exit 1
fi

echo "Restoring $DUMP into Docker db service ($PG_DB as $PG_USER)..."
docker compose exec -T db pg_restore \
  --clean --if-exists --no-owner --no-acl \
  -U "$PG_USER" \
  -d "$PG_DB" \
  < "$DUMP"

echo "Done. Dev DB restored from $DUMP"
