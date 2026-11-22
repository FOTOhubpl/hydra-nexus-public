# HYDRA NEXUS runtime environment — source it, do not execute:  . scripts/env.sh
# Identical on macOS and Linux. Everything heavy or sensitive lives OUTSIDE the repo,
# under ~/HydraNexus, so Git never sees contest data, weights or caches (spec §13, §15).

HYDRA_ROOT="${HYDRA_ROOT:-$HOME/HydraNexus}"
HYDRA_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"   # e.g. darwin-arm64, linux-x86_64

export HYDRA_ROOT HYDRA_PLATFORM
export HYDRA_DATA_DIR="${HYDRA_DATA_DIR:-$HYDRA_ROOT/data}"        # contest / fixture inputs (never committed)
export HYDRA_MODELS_DIR="${HYDRA_MODELS_DIR:-$HYDRA_ROOT/models}"  # approved weights + checksums
export HYDRA_CACHE_DIR="${HYDRA_CACHE_DIR:-$HYDRA_ROOT/cache/$HYDRA_PLATFORM}"  # separate macOS / Linux caches
export HYDRA_STATE_DIR="${HYDRA_STATE_DIR:-$HYDRA_ROOT/state}"     # SQLite WAL + CAS for local runs

export HF_HOME="$HYDRA_CACHE_DIR/huggingface"
export TORCH_HOME="$HYDRA_CACHE_DIR/torch"
export HF_HUB_DISABLE_TELEMETRY=1

# Network policy (ADR-003/007/008, audit A09). HYDRA_OFFLINE=1 is the hard kill switch: no live enrichment, no live
# LLM. Live public-metadata lookups need HYDRA_ENRICH=1 (then HYDRA_OFFLINE defaults to 0). Model downloads from
# Hugging Face are a separate, explicit step: HYDRA_MODEL_DOWNLOAD=1 (just models-fetch).
# These flags are not a firewall; they stop our code and the libraries from reaching out on their own.
if [ "${HYDRA_ENRICH:-0}" = 1 ] || [ "${HYDRA_LLM:-}" = 1 ]; then
  export HYDRA_OFFLINE="${HYDRA_OFFLINE:-0}"
else
  export HYDRA_OFFLINE="${HYDRA_OFFLINE:-1}"
fi
if [ "${HYDRA_MODEL_DOWNLOAD:-0}" = 1 ]; then
  unset HF_HUB_OFFLINE TRANSFORMERS_OFFLINE
else
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
fi

# The API binds loopback only; reach a remote copy through an SSH tunnel.
export HYDRA_API_HOST=127.0.0.1
export HYDRA_API_PORT="${HYDRA_API_PORT:-8000}"

mkdir -p "$HYDRA_DATA_DIR" "$HYDRA_MODELS_DIR" "$HF_HOME" "$TORCH_HOME" "$HYDRA_STATE_DIR"
