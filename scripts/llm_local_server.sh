#!/usr/bin/env bash
# Local Bielik model behind llama.cpp `llama-server` (MIT), OpenAI-compatible, loopback only (ADR-008).
#
#   scripts/llm_local_server.sh <model.gguf> [port=8090] [alias=file name] [threads=auto] [ctx=8192]
#   HYDRA_LLM=1 HYDRA_LLM_BACKEND=local HYDRA_LLM_LOCAL_URL=http://127.0.0.1:8090/v1 uv run python -m hydra.llm status
#
# Binary: $LLAMA_SERVER, else `llama-server` on PATH, else ~/HydraNexus/tools/llama.cpp-*/llama-*/llama-server
# (release b12606: llama-b12606-bin-{ubuntu-x64,macos-arm64}.tar.gz from github.com/ggml-org/llama.cpp).
# Models: ~/HydraNexus/models-llm/ (docs/genes/LLM.md §7, revisions and sha256 pinned there).
# macOS uses Metal automatically (all layers offloaded with -ngl 999); Linux runs on CPU.
# Bielik uses ChatML. The built-in `chatml` template is used with --no-jinja: with the jinja path, b12606 prefixes the
# JSON-schema grammar with the generation prompt and fails ("Failed to initialize samplers", measured 2026-11-21).
# Extra flags: LLAMA_ARGS="...".
set -euo pipefail
model="${1:?usage: $0 <model.gguf> [port] [alias] [threads] [ctx]}"
port="${2:-8090}"
alias="${3:-$(basename "$model" .gguf | tr '[:upper:]' '[:lower:]')}"
threads="${4:-}"
ctx="${5:-8192}"
bin="${LLAMA_SERVER:-$(command -v llama-server || ls -d "$HOME"/HydraNexus/tools/llama.cpp-*/llama-*/llama-server 2>/dev/null | tail -1)}"
[ -x "$bin" ] || { echo "llama-server not found (set LLAMA_SERVER)" >&2; exit 1; }
args=(--model "$model" --alias "$alias" --host 127.0.0.1 --port "$port" --ctx-size "$ctx" --parallel 1
      --no-webui --seed 7 -ngl 999 --no-jinja --chat-template chatml)
# shellcheck disable=SC2206
[ -n "${LLAMA_ARGS:-}" ] && args+=(${LLAMA_ARGS})
[ -n "$threads" ] && args+=(--threads "$threads")
exec "$bin" "${args[@]}"
