#!/usr/bin/env bash
# HYDRA NEXUS "od A do Z": every gate in one command, one PASS/PARTIAL/SKIP/FAIL table with timings.
#
#   bash scripts/e2e_all.sh                 # dev profile: everything; exit non-zero only on a FAIL
#   bash scripts/e2e_all.sh --skip check    # skip steps by name (repeatable): check e2e ui-build ui sample security
#                                           #   adversarial pipeline_audit
#   bash scripts/e2e_all.sh --release       # release profile: all eight steps are required; any SKIP, PARTIAL or
#                                           #   missing step, or a dirty tree, gives OVERALL: PARTIAL and exit 3
#   E2E_ALL_LOG=/tmp/x bash scripts/e2e_all.sh   # log dir (default /tmp/hydra-e2e-all-<timestamp>)
#
# Step result: PASS = exit 0 with passing tests; PARTIAL = exit 0 but xfailed / flaky / did-not-run tests (known
# defects pinned by a CR); SKIP = skipped by --skip, absent, or every test skipped; FAIL = non-zero exit.
# Exit codes: 0 PASS (dev: no FAIL), 1 FAIL, 3 PARTIAL (release profile only).
# The release profile also writes junit XML per pytest step, $LOG/SKIP_XFAIL.md (scripts/skip_report.py with the
# runtime outcomes) and $LOG/summary.json (profile, commit, dirty flag, steps, overall); the dev profile writes
# summary.json too.
#
# Isolation: the e2e API and the Playwright suite start their own API on a free loopback port with a temp state
# (tests/e2e/server.py); nothing here writes to ~/HydraNexus/state or touches the demo ports 8000/8100.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 2
LOG="${E2E_ALL_LOG:-/tmp/hydra-e2e-all-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$LOG"
SKIP=" " PROFILE=dev
REQUIRED=(check e2e ui-build ui sample security adversarial pipeline_audit)
while [ $# -gt 0 ]; do
  case "$1" in
    --skip) SKIP="$SKIP$2 "; shift 2 ;;
    --release) PROFILE=release; shift ;;
    --profile) PROFILE="$2"; shift 2 ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$PROFILE" in dev|release) ;; *) echo "unknown profile: $PROFILE (dev|release)" >&2; exit 2 ;; esac

# The heavy sample regression must not starve the host (CLAUDE.md); the e2e server uses the same caps.
export HYDRA_TORCH_THREADS="${HYDRA_TORCH_THREADS:-2}" OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-2}" HYDRA_OCR_WORKERS="${HYDRA_OCR_WORKERS:-1}"
export HYDRA_OFFLINE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

NAMES=() STATUS=() SECS=() NOTES=()

junit_arg() {  # release profile: the --junitxml flag for a pytest step (consumed by scripts/skip_report.py)
  if [ "$PROFILE" = release ]; then
    printf -- '--junitxml=%s' "$LOG/$1-junit.xml"
  fi
}

summary_of() {  # last pytest / playwright summary line of a log
  local clean
  clean="$(sed -E 's/\x1b\[[0-9;]*m//g' "$1")"
  {
    grep -aE '[0-9]+ (passed|failed|skipped|error|errors).* in [0-9.]+s' <<<"$clean" | tail -1     # pytest
    grep -aE '^ *Tests +[0-9]+' <<<"$clean" | tail -1                                           # vitest
    grep -aE '^ +[0-9]+ (passed|failed|skipped|flaky|did not run)( |$)' <<<"$clean" | tail -4   # playwright
  } | sed -E 's/^[ =]+//; s/[ =]+$//; s/  +/ /g' | paste -sd ';' - | sed 's/;/; /g' | cut -c1-120
}

count_of() {  # count_of "<summary>" "<word>" -> sum of the numbers in front of the word (0 when absent)
  grep -oE "[0-9]+ $2" <<<"$1" | awk '{s += $1} END {print s + 0}'
}

run_step() {  # name, command...
  local name="$1"; shift
  if [[ "$SKIP" == *" $name "* ]]; then
    NAMES+=("$name") STATUS+=("SKIP") SECS+=("-") NOTES+=("--skip")
    return
  fi
  local log="$LOG/$name.log" t0 rc st note defects
  printf '>>> %-15s %s\n' "$name" "$*"
  t0=$(date +%s)
  ( "$@" ) >"$log" 2>&1
  rc=$?
  note="$(summary_of "$log")"
  if [ $rc -eq 0 ]; then
    st=PASS
    if [[ "$note" =~ skipped ]] && ! [[ "$note" =~ passed ]]; then st=SKIP; fi
    # known defects (strict xfail / test.fail), flaky or not-run tests: the step did not fully pass
    defects=$(( $(count_of "$note" xfailed) + $(count_of "$note" flaky) + $(count_of "$note" 'did not run') ))
    if [ "$st" = PASS ] && [ "$defects" -gt 0 ]; then st=PARTIAL; fi
  else
    st=FAIL
    [ -n "$note" ] || note="exit $rc, see $log"
  fi
  NAMES+=("$name") STATUS+=("$st") SECS+=("$(( $(date +%s) - t0 ))") NOTES+=("$note")
  printf '<<< %-15s %s (%ss)\n' "$name" "$st" "${SECS[${#SECS[@]}-1]}"
}

run_step check        env PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} $(junit_arg check)" just check
# shellcheck disable=SC2046  # junit_arg prints one flag or nothing
run_step e2e          uv run pytest -m e2e tests/e2e -p no:cacheprovider -rxXs $(junit_arg e2e)
run_step ui-build     bash -c 'cd ui && pnpm -s build'
if [ -f ui/e2e/run.sh ]; then
  run_step ui         bash ui/e2e/run.sh
else
  NAMES+=(ui) STATUS+=(FAIL) SECS+=(-) NOTES+=("ui/e2e/run.sh missing")
fi
# shellcheck disable=SC2046
run_step sample       env HYDRA_EXTRACT_WORKERS=0 HYDRA_SAMPLE_TEST=1 uv run pytest -q -rs \
                          tests/core/test_sample_regression.py $(junit_arg sample)
# shellcheck disable=SC2046
run_step security     uv run pytest -q -rsx tests/security $(junit_arg security)
# shellcheck disable=SC2046
run_step adversarial  uv run pytest -q -rsx tests/adversarial $(junit_arg adversarial)
if [ -d tests/pipeline_audit ]; then
  # shellcheck disable=SC2046
  run_step pipeline_audit uv run pytest -q -rsx tests/pipeline_audit $(junit_arg pipeline_audit)
else
  NAMES+=(pipeline_audit) STATUS+=(SKIP) SECS+=(-) NOTES+=("tests/pipeline_audit not present")
fi

# Release profile: every required step must have run and passed; anything else is PARTIAL, never PASS.
SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')"
MISSING=()
if [ "$PROFILE" = release ]; then
  for req in "${REQUIRED[@]}"; do
    found=0
    for n in "${NAMES[@]}"; do [ "$n" = "$req" ] && found=1; done
    if [ $found -eq 0 ]; then
      MISSING+=("$req")
      NAMES+=("$req") STATUS+=(SKIP) SECS+=(-) NOTES+=("required step not run")
    fi
  done
fi

fail=0 partial=0 nskip=0 npartial=0
dirty_tag=""
[ "$DIRTY" != 0 ] && dirty_tag="+dirty($DIRTY)"
echo
echo "HYDRA NEXUS e2e-all [$PROFILE]  ${SHA:0:7}$dirty_tag on $(hostname -s)  $(date '+%Y-%m-%d %H:%M %Z')  logs: $LOG"
printf '%-15s | %-7s | %7s | %s\n' STEP RESULT SECONDS DETAIL
printf -- '----------------+---------+---------+-----------------------------------------------\n'
for i in "${!NAMES[@]}"; do
  printf '%-15s | %-7s | %7s | %s\n' "${NAMES[$i]}" "${STATUS[$i]}" "${SECS[$i]}" "${NOTES[$i]}"
  case "${STATUS[$i]}" in
    FAIL) fail=1 ;;
    SKIP) nskip=$((nskip + 1)) ;;
    PARTIAL) npartial=$((npartial + 1)) ;;
  esac
done
echo
if [ "$PROFILE" = release ]; then
  [ $((nskip + npartial)) -gt 0 ] && partial=1
  if [ "$DIRTY" != 0 ]; then
    partial=1
    echo "release: $DIRTY modified tracked file(s); these results do not describe commit ${SHA:0:7}"
  fi
  existing=()
  for j in "$LOG"/*-junit.xml; do [ -f "$j" ] && existing+=("$j"); done
  uv run python scripts/skip_report.py --out "$LOG/SKIP_XFAIL.md" --junit ${existing[@]+"${existing[@]}"} \
    || echo "release: scripts/skip_report.py failed"
fi
if [ $fail -ne 0 ]; then overall=FAIL; rc=1
elif [ $partial -ne 0 ]; then overall=PARTIAL; rc=3
else overall=PASS; rc=0
fi

json_str() { local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; printf '"%s"' "$s"; }
{
  printf '{"profile": "%s", "commit": "%s", "dirty_tracked_files": %s, "host": %s, "finished": "%s", ' \
    "$PROFILE" "$SHA" "$DIRTY" "$(json_str "$(hostname -s)")" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '"overall": "%s", "required": [' "$overall"
  sep=""
  for r in "${REQUIRED[@]}"; do printf '%s"%s"' "$sep" "$r"; sep=", "; done
  printf '], "steps": ['
  sep=""
  for i in "${!NAMES[@]}"; do
    printf '%s{"step": "%s", "result": "%s", "seconds": "%s", "detail": %s}' "$sep" \
      "${NAMES[$i]}" "${STATUS[$i]}" "${SECS[$i]}" "$(json_str "${NOTES[$i]}")"
    sep=", "
  done
  printf ']}\n'
} >"$LOG/summary.json"

if [ "$overall" = PASS ] && [ $((nskip + npartial)) -gt 0 ]; then
  echo "OVERALL: PASS (dev profile: $nskip SKIP, $npartial PARTIAL; the submission gate is --release)"
else
  echo "OVERALL: $overall"
fi
if [ "$overall" = PARTIAL ]; then
  echo "release: required steps not fully passed (SKIP=$nskip PARTIAL=$npartial${MISSING[0]+ missing: ${MISSING[*]}})"
fi
exit $rc
