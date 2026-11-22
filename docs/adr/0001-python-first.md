# ADR-001: P0 in Python
Status: Proposed (spec v2.0 §5)

Decision: the P0 engine is Python 3.11 (uv, Pydantic, FastAPI on loopback). Rust/PyO3 only later, behind
the same Observation contract — no parallel Rust and Python implementations during the hackathon.

Why: one implementation fits a 24h window; the shared contract keeps a later port possible.
