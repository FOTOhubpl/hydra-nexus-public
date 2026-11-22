#!/usr/bin/env bash
# Time read endpoints over HTTP (as the browser sees them: gzip accepted) against a running API.
#   scripts/bench_http.sh <base-url> <run_id> [reps]
# Prints: endpoint, rep, HTTP status, total seconds, bytes on the wire, cache header.
set -euo pipefail
base=${1:?base url, e.g. http://127.0.0.1:8600}
run=${2:?run id}
reps=${3:-2}
eps=(
  "explore /v1/graph/explore?run_id=$run"
  "overview /v1/graph/overview?run_id=$run"
  "flow /v1/system/flow?run_id=$run"
  "dashboard /v1/dashboard?run_id=$run"
  "analytics /v1/analytics/overview?run_id=$run"
  "dashgraph /v1/graph?run_id=$run&min_p=0&min_bits=10&links_only=true"
  "runs /v1/runs"
  "campaigns /v1/campaigns?run_id=$run"
  "pairs /v1/pairs?run_id=$run"
  "incidents /v1/incidents?run_id=$run"
)
for e in "${eps[@]}"; do
  name=${e%% *}
  path=${e#* }
  for i in $(seq 1 "$reps"); do
    out=$(curl -s -o /dev/null -D /tmp/bench_http_headers.$$ -H 'Accept-Encoding: gzip' \
      -w '%{http_code} %{time_total} %{size_download}' "$base$path")
    cache=$(grep -i '^x-hydra-cache' /tmp/bench_http_headers.$$ | tr -d '\r' | awk '{print $2}' || true)
    printf '%-10s rep%s %s cache=%s\n' "$name" "$i" "$out" "${cache:--}"
  done
done
rm -f /tmp/bench_http_headers.$$
