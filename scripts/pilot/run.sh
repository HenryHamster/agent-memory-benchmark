#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
test -x "$ROOT/.pilot/amb-venv/bin/python" || { echo 'Run bash scripts/pilot/setup.sh first.' >&2; exit 1; }
exec "$ROOT/.pilot/amb-venv/bin/python" "$ROOT/scripts/pilot/run.py" "$@"
