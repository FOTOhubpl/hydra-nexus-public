#!/usr/bin/env bash
# Exit 0 when the scoring paths are byte-identical between the scoring-frozen SHA and the final SHA (so the
# published evaluation of the frozen SHA also describes the final product). usage: check_scoring_frozen.sh <frozen> <final>
set -euo pipefail
git diff --quiet "$1" "$2" -- hydra/synapse.py hydra/entropy.py hydra/assign.py hydra/conformal.py hydra/nexus.py \
  hydra/retrieval.py hydra/priors hydra/genome hydra/pipeline.py hydra/batch.py && echo "scoring paths unchanged $1..$2" || { echo "SCORING PATHS CHANGED $1..$2"; git diff --stat "$1" "$2" -- hydra/synapse.py hydra/entropy.py hydra/assign.py hydra/conformal.py hydra/nexus.py hydra/retrieval.py hydra/priors hydra/genome hydra/pipeline.py hydra/batch.py; exit 1; }
