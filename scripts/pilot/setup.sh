#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="$ROOT/.pilot"
PYTHON="${PILOT_BOOTSTRAP_PYTHON:-python3}"
command -v git >/dev/null
command -v "$PYTHON" >/dev/null
test "$(uname -s)" = Linux || { echo 'This recipe is tested on Linux / Ubuntu WSL2.' >&2; exit 1; }
mkdir -p "$STATE/uv-bootstrap"
UV="$STATE/uv-bootstrap/bin/uv"
if ! test -x "$UV"; then
    "$PYTHON" -m pip install --disable-pip-version-check --no-deps --target "$STATE/uv-bootstrap" 'uv==0.11.3'
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-$STATE/uv-cache}"
export UV_LINK_MODE=copy

pin_repo() {
    local url="$1" path="$2" revision="$3"
    if ! test -d "$path"; then
        git init "$path"
        git -C "$path" remote add origin "$url"
        git -C "$path" fetch --depth 1 origin "$revision"
    fi
    test -z "$(git -C "$path" status --porcelain)" || { echo "Preserving changed checkout: $path" >&2; exit 1; }
    if ! git -C "$path" cat-file -e "$revision^{commit}" 2>/dev/null; then git -C "$path" fetch --depth 1 origin "$revision"; fi
    git -C "$path" checkout --detach "$revision"
}
pin_repo https://github.com/HenryHamster/hindsight "$STATE/hindsight" f7dd3f4fd7420f7beec60c32c965e5e5cf7be066
pin_repo https://github.com/xiaowu0162/LongMemEval "$STATE/LongMemEval" 9e0b455f4ef0e2ab8f2e582289761153549043fc

for env in hindsight amb; do
    test -x "$STATE/$env-venv/bin/python" || "$UV" venv --python 3.12 "$STATE/$env-venv"
    "$UV" pip install --python "$STATE/$env-venv/bin/python" --no-deps -r "$ROOT/scripts/pilot/$env-requirements.txt"
done
"$UV" pip install --python "$STATE/hindsight-venv/bin/python" --no-deps -e "$STATE/hindsight/hindsight-api-slim[embedded-db,local-onnx]"
"$UV" pip install --python "$STATE/amb-venv/bin/python" --no-deps -e "$ROOT[hindsight-http]"
"$UV" pip check --python "$STATE/hindsight-venv/bin/python"
"$UV" pip check --python "$STATE/amb-venv/bin/python"
"$STATE/amb-venv/bin/python" "$ROOT/scripts/pilot/prepare_data.py"
"$STATE/amb-venv/bin/python" -m pytest "$ROOT/tests/test_pilot.py" -q
echo 'Setup complete. Next: bash scripts/pilot/run.sh --api vertex-express'
