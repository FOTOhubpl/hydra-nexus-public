# ADR-003: No external inference
Status: Proposed (spec v2.0 §5)

Decision: HYDRA's runtime uses only local models and never calls commercial inference APIs. `scripts/env.sh`
sets the HF/Transformers offline flags by default; downloads happen only with `HYDRA_OFFLINE=0` after approval.

Why: the product must work offline and must not send contest data out. Claude Code is a development tool,
not part of the runtime.
