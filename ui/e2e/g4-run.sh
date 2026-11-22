#!/usr/bin/env bash
# G4 scenarios (internal G4 scenario notes, not published) against our own g4 server. Usage:
#   ui/e2e/g4-run.sh ROOT [extra playwright args]
# ROOT is a fresh directory (deleted first). Starts tests/e2e/g4_server.py (auth on, own admin password, synthetic
# corpus analysed, models from HYDRA_MODELS_DIR or ~/HydraNexus/models), runs ui/e2e/g4-*.spec.ts serially, then
# stops the server it started (pid of the restarted server from ROOT/server.json). Never touches 8000/8100/18100.
set -euo pipefail
E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$E2E_DIR/../.." && pwd)"
ROOT="${1:?usage: g4-run.sh ROOT}"; shift
PY="${G4_PYTHON:-$WT/.venv/bin/python}"
OUT="${G4_OUT_DIR:-$(dirname "$ROOT")}"
rm -rf "$ROOT"; mkdir -p "$OUT"
export HYDRA_MODELS_DIR="${HYDRA_MODELS_DIR:-$HOME/HydraNexus/models}"
(cd "$WT" && "$PY" -m tests.e2e.g4_server --root "$ROOT" --ready "$ROOT/server.json" --ui-dist ui/dist >"$OUT/launcher.log" 2>&1 &)
for _ in $(seq 1 600); do [[ -f "$ROOT/server.json" ]] && break; sleep 0.5; done
sleep 0.5
BASE="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["base_url"])' "$ROOT/server.json")"
PKG="${PLAYWRIGHT_PKG:-$(readlink "$E2E_DIR/node_modules/playwright" || true)}"
set +e
(cd "$E2E_DIR" && E2E_BASE_URL="$BASE" E2E_SERVER_JSON="$ROOT/server.json" G4_OUT_DIR="$OUT" E2E_OUT_DIR="$OUT" G4_PYTHON="$PY" \
  node "$PKG/cli.js" test -c g4.config.ts "$@")
rc=$?
set -e
PID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["pid"])' "$ROOT/server.json" 2>/dev/null || true)"
[[ -n "$PID" ]] && kill -TERM "$PID" 2>/dev/null || true
exit $rc
