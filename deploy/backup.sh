#!/usr/bin/env bash
# Back up PostgreSQL (source of truth for accounts, jobs, history) and, optionally, the media volume.
#   ./deploy/backup.sh [OUTPUT_DIR]           database only (recommended; media is transient and expires)
#   WITH_MEDIA=1 ./deploy/backup.sh [DIR]     database + media volume
# Override the compose invocation with COMPOSE="docker compose -f ... --env-file ..." (default: docker compose).
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-docker compose}
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${1:-backups}"; OUT="$(cd "${1:-backups}" && pwd)"; chmod 700 "$OUT"
$COMPOSE exec -T db pg_dump -U ytaria -d ytaria --format=custom --no-owner > "$OUT/db-$STAMP.dump"
chmod 600 "$OUT/db-$STAMP.dump"
echo "database  -> $OUT/db-$STAMP.dump ($(du -h "$OUT/db-$STAMP.dump" | cut -f1))"
if [ "${WITH_MEDIA:-0}" = "1" ]; then
  VOL="$($COMPOSE config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')_media"
  docker run --rm -v "$VOL":/data:ro -v "$OUT":/backup alpine tar czf "/backup/media-$STAMP.tar.gz" -C /data .
  chmod 600 "$OUT/media-$STAMP.tar.gz"
  echo "media     -> $OUT/media-$STAMP.tar.gz"
fi
