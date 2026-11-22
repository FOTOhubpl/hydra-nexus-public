# ADR-004: Cloud optional
Status: Proposed (spec v2.0 §5)

Decision: the Linux server is an accelerator, not a dependency. Every pipeline stage must run on the laptop's
CPU profile with identical result contracts.

Why: if cloud use is disallowed by the rules, or the network fails at the venue, the demo still runs.
