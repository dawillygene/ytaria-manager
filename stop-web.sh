#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/ytaria.py" stop --host 127.0.0.1 --port 8787 "$@"
