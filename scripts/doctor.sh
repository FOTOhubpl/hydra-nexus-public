#!/usr/bin/env bash
# HYDRA NEXUS doctor — read-only inventory of hardware and tool versions (spec §13).
# Same script on macOS (operator laptop) and Linux (the Linux server). Changes nothing
# except creating the empty data/model/cache dirs that scripts/env.sh declares.
set -u
cd "$(dirname "$0")/.." || exit 1

section() { printf '\n== %s ==\n' "$1"; }
missing=0

section "host"
printf 'hostname: %s\n' "$(hostname)"
uname -m
if command -v sw_vers >/dev/null; then sw_vers; else grep -E '^PRETTY_NAME=' /etc/os-release; fi
df -h .

section "cpu / ram / gpu"
if [ "$(uname -s)" = Darwin ]; then
  printf 'cpus: %s\n' "$(sysctl -n hw.ncpu)"
  printf 'ram_gib: %s\n' "$(( $(sysctl -n hw.memsize) / 1073741824 ))"
  system_profiler SPDisplaysDataType 2>/dev/null | grep -E 'Chipset Model|Metal|Total Number of Cores' | sed 's/^ *//'
else
  printf 'cpus: %s\n' "$(nproc)"
  free -h | head -2
  if command -v nvidia-smi >/dev/null; then
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  else
    echo 'gpu: none (no nvidia-smi) -> CPU profile'
  fi
fi

section "tools"
for tool in git git-lfs gh uv node pnpm just tmux claude ffmpeg tesseract rg jq; do
  if command -v "$tool" >/dev/null; then
    printf '%-10s ' "$tool"
    case "$tool" in
      tmux) tmux -V ;;
      ffmpeg) ffmpeg -version 2>&1 | head -n 1 ;;
      *) "$tool" --version 2>&1 | head -n 1 ;;
    esac
  else
    printf 'MISSING: %s\n' "$tool"; missing=$((missing + 1))
  fi
done

section "python (uv-managed)"
if command -v uv >/dev/null; then
  if py=$(uv python find 3.11 2>/dev/null); then
    printf '%s  ' "$py"; "$py" -V
  else
    echo 'MISSING: uv python 3.11 (run: uv python install 3.11)'; missing=$((missing + 1))
  fi
fi

section "tesseract languages (need pol + eng)"
if command -v tesseract >/dev/null; then
  langs=$(tesseract --list-langs 2>/dev/null | tail -n +2)
  for l in pol eng; do
    if printf '%s\n' "$langs" | grep -qx "$l"; then echo "ok: $l"; else echo "MISSING: tesseract lang $l"; missing=$((missing + 1)); fi
  done
fi

section "claude"
if command -v claude >/dev/null; then
  claude --version
  echo "(run 'claude doctor' interactively for the full check)"
fi

section "environment (scripts/env.sh)"
# shellcheck source=/dev/null
. scripts/env.sh
env | grep -E '^(HYDRA_|HF_|TRANSFORMERS_|TORCH_HOME)' | sort

section "summary"
if [ "$missing" -eq 0 ]; then echo "doctor: all required tools present"; else echo "doctor: $missing item(s) missing"; fi
exit 0
