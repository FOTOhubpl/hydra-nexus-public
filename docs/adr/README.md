# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [001](0001-python-first.md) | P0 in Python, no dual Rust/Python implementation | Proposed (spec v2.0 §5) |
| [002](0002-single-sqlite-writer.md) | Single SQLite writer | Proposed (spec v2.0 §5) |
| [003](0003-no-external-inference.md) | No external inference | Proposed (spec v2.0 §5) |
| [004](0004-cloud-optional.md) | Cloud optional, never a critical dependency | Proposed (spec v2.0 §5) |
| [007](0007-online-enrichment-layer.md) | Online enrichment layer with a replayable evidence cache | Accepted (2026-11-21, task §3/§5); amends 003/004 |
| [008](0008-optional-llm-analyst.md) | Optional LLM analyst layer (Claude on Bedrock), cached and replayable | Accepted (2026-11-21); amends 003 for this layer only |
| [009](0009-incremental-dataflow.md) | Incremental, event-driven per-run aggregates and a live stream (SSE) | Proposed (DATAFLOW, 2026-11-21); migration v5 for the architect's review |

ADR 001–004 are recorded as written in the design specification. ADR 005 and 006 (internal code hosting) are not published.
