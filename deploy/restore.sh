#!/usr/bin/env bash
# Restore a database dump made by backup.sh. DESTRUCTIVE: replaces the current database contents.
#   ./deploy/restore.sh backups/db-YYYYmmddTHHMMSSZ.dump
# Stops api/worker/scheduler, restores, runs migrations (so an older dump is brought up to date), restarts.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-docker compose}
DUMP="${1:?usage: restore.sh DUMP_FILE}"
[ -f "$DUMP" ] || { echo "no such file: $DUMP" >&2; exit 1; }
if [ "${ASSUME_YES:-0}" != "1" ]; then
  read -r -p "This REPLACES the current database with $DUMP. Type 'restore' to continue: " ans
  [ "$ans" = "restore" ] || { echo "aborted"; exit 1; }
fi
$COMPOSE stop api worker scheduler web
$COMPOSE up -d db
$COMPOSE exec -T db sh -c 'until pg_isready -U ytaria -d ytaria; do sleep 1; done'
$COMPOSE exec -T db pg_restore -U ytaria -d ytaria --clean --if-exists --no-owner < "$DUMP"
$COMPOSE run --rm migrate
$COMPOSE up -d
echo "restore complete"
