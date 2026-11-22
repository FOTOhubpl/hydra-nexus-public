#!/usr/bin/env bash
# Runs the Playwright UI end-to-end suite without adding an npm dependency.
#
# Resolves a Playwright >= 1.65 package (ui/node_modules first, then the npx cache, 1.65.0 preferred), links it as
# ui/e2e/node_modules/playwright (ignored by the root .gitignore `node_modules/`) so `import ... from
# "playwright/test"` resolves for the ESM specs, then runs `playwright test -c ui/e2e/playwright.config.ts`.
# Extra args go to playwright, e.g. `ui/e2e/run.sh --project=desktop-light routes`.
#
# Env: E2E_BASE_URL  reuse an already-running tests.e2e.server (otherwise globalSetup starts one, ~1 min);
#      E2E_BUILD=0   skip the UI build (default: `pnpm build` when ui/dist is missing or older than ui/src);
#      PLAYWRIGHT_PKG force a Playwright package dir.
set -euo pipefail
E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI_DIR="$(dirname "$E2E_DIR")"

version_of() { node -p "require(process.argv[1] + '/package.json').version" "$1" 2>/dev/null || echo 0; }
ok_version() { node -e "const [a,b]=process.argv[1].split('.').map(Number);process.exit(a>1||(a===1&&b>=65)?0:1)" "$1"; }

pick_pkg() {
  if [[ -n "${PLAYWRIGHT_PKG:-}" ]]; then echo "$PLAYWRIGHT_PKG"; return; fi
  local cand v best="" fallback=""
  for cand in "$UI_DIR/node_modules/playwright" "$HOME"/.npm/_npx/*/node_modules/playwright; do
    [[ -f "$cand/package.json" && -f "$cand/cli.js" ]] || continue
    cand="$(cd "$cand" && pwd -P)"
    v="$(version_of "$cand")"
    ok_version "$v" || continue
    if [[ "$v" == "1.65.0" && -z "$best" ]]; then best="$cand"; fi
    if [[ -z "$fallback" && "$v" != *-* ]]; then fallback="$cand"; fi
  done
  echo "${best:-$fallback}"
}

PKG="$(pick_pkg)"
if [[ -z "$PKG" ]]; then
  echo "run.sh: no Playwright >= 1.65 found (ui/node_modules or ~/.npm/_npx); run 'npx playwright@1.65.0 --version' once" >&2
  exit 2
fi
mkdir -p "$E2E_DIR/node_modules"
ln -sfn "$PKG" "$E2E_DIR/node_modules/playwright"

if [[ "${E2E_BUILD:-1}" != "0" && -z "${E2E_BASE_URL:-}" ]]; then
  if [[ ! -f "$UI_DIR/dist/index.html" ]] || [[ -n "$(find "$UI_DIR/src" -newer "$UI_DIR/dist/index.html" -type f -print -quit)" ]]; then
    (cd "$UI_DIR" && pnpm -s build)
  fi
fi

echo "run.sh: playwright $(version_of "$PKG") from $PKG" >&2
cd "$E2E_DIR"
exec node "$PKG/cli.js" test -c "$E2E_DIR/playwright.config.ts" "$@"
