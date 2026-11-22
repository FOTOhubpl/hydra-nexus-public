# HYDRA NEXUS task runner — same recipes on macOS and Linux.
set shell := ["bash", "-c"]
# macOS: resolve wheels for macOS 15+ (otherwise uv picks older torch/opencv builds). Ignored on Linux.
export MACOSX_DEPLOYMENT_TARGET := "15.0"

default:
    @just --list

# Hardware + tool inventory (read-only)
doctor:
    bash scripts/doctor.sh

# Print the runtime environment HYDRA uses (offline flags, cache/data dirs)
env:
    @bash -c '. scripts/env.sh && env | grep -E "^(HYDRA_|HF_|TRANSFORMERS_|TORCH_HOME)" | sort'

# Install the pinned Python toolchain into .venv
setup:
    uv python install 3.11
    uv sync --locked

# Quality gate (spec §17): Python lint + tests, UI typecheck + tests when ui/ exists
check:
    uv run ruff check .
    uv run pytest -q
    if [ -f ui/package.json ]; then cd ui && pnpm -s typecheck && pnpm -s test; fi

# Generic library/throughput smoke: temp state, ZIP upload -> job -> audit -> DINOv2/e5 -> top-k. Args: --http --images N
smoke *args:
    . scripts/env.sh && uv run python scripts/smoke_e2e.py {{args}}

# API on 127.0.0.1:8000 (loopback only); env from scripts/env.sh
serve:
    . scripts/env.sh && uv run hydra serve

# UI dev server on 127.0.0.1:5173, proxies /api -> 127.0.0.1:8000
ui:
    cd ui && pnpm dev

# Import files/folders/ZIPs without the server:  just import ~/HydraNexus/data/sample
import +paths:
    . scripts/env.sh && uv run hydra import {{paths}}

# Re-hash all originals + verify the audit hash chain
verify:
    . scripts/env.sh && uv run hydra verify

# Download/verify offline models (download needs network: HYDRA_OFFLINE=0)
models-fetch:
    HYDRA_MODEL_DOWNLOAD=1 HYDRA_OFFLINE=0 bash -c '. scripts/env.sh && uv run python scripts/fetch_models.py && uv run python scripts/fetch_misp_warninglists.py --commit 40e2b570fda2e5443a2d6fa1d1db16c37ab0a956'
models-verify:
    . scripts/env.sh && uv run python scripts/fetch_models.py --verify

# Docker, one-time (needs network): pinned models + CERT/PSL into ~/HydraNexus, then offline hash + smoke verify
docker-models:
    mkdir -p ~/HydraNexus/{models,data/external,cache/linux-x86_64,cache/enrich,state-docker,exports}
    docker compose build api && docker compose --profile models run --rm models

# Docker jury flow, no network (network_mode none): analyse a known base, assign a folder of new items, export JSON/CSV.
# Paths are relative to ~/HydraNexus/data:  just docker-jury nask/sample jury/new jury-run1
docker-jury base new name="jury":
    docker compose stop api 2>/dev/null || true
    docker compose --profile cli run --rm --entrypoint python cli scripts/build_vis_reference.py --sample /hydra/data/{{base}} --out /hydra/state/reference
    docker compose --profile cli run --rm cli analyse /hydra/data/{{base}}
    docker compose --profile cli run --rm cli assign-batch /hydra/data/{{new}} --out /hydra/exports/{{name}}
    @echo "results: ~/HydraNexus/exports/{{name}}/assignments.csv (GUI: docker compose up -d, http://127.0.0.1:8000/)"

# GUI without host networking (Docker Desktop on macOS/Windows): port published on 127.0.0.1:8000 only
docker-gui:
    docker compose stop api 2>/dev/null || true
    docker compose --profile gui-ports up -d gui && docker compose --profile gui-ports ps gui

# Server: build + run the API container (host network, loopback bind)
docker-up:
    docker compose up -d --build && docker compose ps

# Move the local state (SQLite + CAS + work files) aside to a timestamped backup; never deletes
reset-state:
    . scripts/env.sh && if [ -d "$HYDRA_STATE_DIR" ]; then mv "$HYDRA_STATE_DIR" "$HYDRA_STATE_DIR.bak-$(date +%Y%m%d-%H%M%S)" && echo "state moved aside"; else echo "no state"; fi

# Demo: API (127.0.0.1:8000) + UI (127.0.0.1:5173) in one terminal; Ctrl+C stops both. Fresh state: just reset-state first
demo:
    #!/usr/bin/env bash
    set -euo pipefail
    . scripts/env.sh
    uv run hydra serve & api=$!
    trap 'kill $api 2>/dev/null' EXIT
    for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break; sleep 0.5; done
    curl -fsS http://127.0.0.1:8000/health && echo
    cd ui && pnpm dev

# BONUS-AUTO offline jury demo: throwaway server (own temp state, free port, HYDRA_OFFLINE=1, SYNTHETIC corpus
# analysed), then /watch?view=auto -> "Demonstracja offline (DANE SYNTETYCZNE)". Never touches :8000 / :8100.
demo-auto root="/tmp/hydra-auto-demo":
    #!/usr/bin/env bash
    set -euo pipefail
    [ -f ui/dist/index.html ] || (cd ui && pnpm -s build)
    mkdir -p "{{root}}"
    echo "starting (about 1 min); then open the base_url printed in {{root}}/server.json, login admin / e2e-Admin-Passw0rd-2026"
    uv run python -m tests.e2e.server --root "{{root}}" --ready "{{root}}/server.json" --ui-dist ui/dist

# Evaluation harness (NASK §6): results.md + charts + runs.metrics. Args: --quick --backend stub|hydra --regen
eval *args:
    . scripts/env.sh && uv run python -W ignore -m eval.run {{args}}

# Merge gate: branch vs main baseline on the same split; fails on lower assignment/open-set, more false merges, crashes
eval-gate *args:
    . scripts/env.sh && uv run python -W ignore -m eval.gate {{args}}

# Copy the latest eval run's metrics from the isolated eval state into the main state (the UI reads it)
eval-publish *args:
    . scripts/env.sh && uv run python -m eval.publish {{args}}

# Optional AI analyst: download one pinned Bielik GGUF (4.5b ~2.9 GB, or 1.5b ~0.97 GB). Not needed for the demo or eval.
llm-fetch model="4.5b":
    . scripts/env.sh && HYDRA_OFFLINE=0 uv run python scripts/llm_fetch.py --model {{model}}

# End-to-end API suite: own API on a free loopback port, temp state, synthetic corpus (never the demo state/ports)
e2e *args:
    uv run pytest -m e2e tests/e2e -p no:cacheprovider -rxXs {{args}}

# Playwright UI suite against its own e2e API serving the built UI (ui/dist)
e2e-ui *args:
    bash ui/e2e/run.sh {{args}}

# Everything, one PASS/PARTIAL/SKIP/FAIL table: check, e2e, Playwright, sample regression, security, adversarial
e2e-all *args:
    bash scripts/e2e_all.sh {{args}}

# Jury path from a clean state, offline: fresh state -> fuzz + synthetic import (GUI upload and path) -> analyse ->
# endpoints/pages 200 -> evidence export (sha256 re-verified) -> audit chain -> CLI batch. Args: --n 20 --keep
jury-smoke *args:
    uv run python scripts/jury_smoke.py {{args}}

# Offline reference-data bundle (MISP warninglists, PSL, CERT Polska; no knf/mf_hazard) next to the image, never in git
reference-data-build out="~/HydraNexus/release":
    . scripts/env.sh && uv run python scripts/reference_data.py build --out {{out}}

# Unpack + verify the bundle into $HYDRA_DATA_DIR/external (SHA256SUMS per set); then `... status`
reference-data-install bundle:
    . scripts/env.sh && uv run python scripts/reference_data.py install {{bundle}}

# Submission gate: all eight steps required; any SKIP/PARTIAL/missing step or a dirty tree -> PARTIAL (exit 3)
release-gate *args:
    bash scripts/e2e_all.sh --release {{args}}

# Inventory of every skip/xfail with CR and owner -> docs/release/SKIP_XFAIL.md (add --junit <xml...> for outcomes)
skip-report *args:
    uv run python scripts/skip_report.py {{args}}

# Alias: one-time download + verification of the pinned weights and reference data (docs/MODELS.md)
models: models-fetch models-verify
