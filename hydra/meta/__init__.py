"""META-COLLECT: automated Meta Ad Library collection and analysis (docs/integrations/META_COLLECTION.md).

contract   pinned Graph API version, requested fields, layered diagnostics
transport  the only Meta HTTP path: own hourly budget, Graph error classes, usage headers, token scrubbing
normalize  Graph row -> normalised content + content hash (observation times excluded)
store      v8 tables (hydra/db.py): config, queries, polls, ads, revisions, outbox, analyses, reviews
collector  one collection cycle: query rotation, pagination with per-query cursors, date-window overlap
media      asset statuses, opt-in snapshot/media acquisition, analyst attachment
analysis   outbox worker: import as ext:meta-adlibrary incident, extraction, open-set assignment, triage,
           manipulation status, report
report     single-ad and period reports from one representation
"""
