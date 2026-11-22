# ADR-002: Single SQLite writer
Status: Proposed (spec v2.0 §5)

Decision: SQLite in WAL mode with exactly one writer process; workers send results to it. Each transaction
writes state and its audit event together.

Why: avoids lock contention and keeps the event log consistent with state.
