"""Append-only audit log with a hash chain.

Each event stores the hash of the previous one, so editing or deleting any past event breaks
`verify_chain`. Call `append` inside the same writer transaction as the state change it records.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

GENESIS = "0" * 64


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept. Used for hashing and manifests."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _event_hash(prev_hash: str, ts: str, type_: str, payload: str) -> str:
    return hashlib.sha256(f"{prev_hash}\n{ts}\n{type_}\n{payload}".encode()).hexdigest()


def append(conn: sqlite3.Connection, type_: str, payload: dict[str, Any]) -> str:
    row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    ts = utcnow()
    body = canonical_json(payload)
    h = _event_hash(prev, ts, type_, body)
    cur = conn.execute(
        "INSERT INTO events (ts, type, payload, prev_hash, hash) VALUES (?, ?, ?, ?, ?)",
        (ts, type_, body, prev, h),
    )
    if type_ in TAP_TYPES:
        _dispatch(conn, int(cur.lastrowid or 0), type_, payload)
    return h


# Event taps (ADR-0009): derived state that must change in the same transaction as the event that causes it
# (materialised aggregates, hydra.dataflow). A tap never fails the caller's write; it handles its own errors.
TAP_TYPES = frozenset({"RUN_FINISHED", "REVIEW", "EXPORT", "REVIEW_UNSURE", "REVIEW_UNSURE_UNDONE"})


def _dispatch(conn: sqlite3.Connection, seq: int, type_: str, payload: dict[str, Any]) -> None:
    from hydra import dataflow  # lazy: hydra.dataflow imports this module

    dataflow.on_event(conn, seq, type_, payload)


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int, int | None]:
    """Returns (ok, events_checked, first_bad_seq)."""
    prev = GENESIS
    n = 0
    for row in conn.execute("SELECT seq, ts, type, payload, prev_hash, hash FROM events ORDER BY seq"):
        n += 1
        if row["prev_hash"] != prev or _event_hash(prev, row["ts"], row["type"], row["payload"]) != row["hash"]:
            return False, n, row["seq"]
        prev = row["hash"]
    return True, n, None
