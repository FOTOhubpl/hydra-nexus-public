"""Job bookkeeping + a bounded executor.

A job is a row in `jobs`; its unit results go to `job_items`. Handlers receive a `JobContext`
to report per-item outcomes. A failing item never stops the job; an exception in the handler
marks the job `failed` with an error code (exception class name) instead of crashing the API.
"""

from __future__ import annotations

import json
import sqlite3
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from hydra import events
from hydra.db import Writer

TERMINAL = {"done", "failed"}


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def create_job(writer: Writer, kind: str, params: dict[str, Any] | None = None) -> str:
    job_id = new_job_id()

    def _tx(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO jobs (id, kind, status, params, created_at) VALUES (?, ?, 'queued', ?, ?)",
            (job_id, kind, events.canonical_json(params or {}), events.utcnow()),
        )
        events.append(conn, "JOB_CREATED", {"job_id": job_id, "kind": kind})

    writer.run(_tx)
    return job_id


def recover_stale_jobs(writer: Writer) -> int:
    """On startup no worker is alive, so queued/running rows are orphans: mark them failed (visible, not stuck)."""

    def _tx(conn: sqlite3.Connection) -> int:
        ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running')")]
        for job_id in ids:
            conn.execute(
                "UPDATE jobs SET status='failed', error_code='INTERRUPTED', error=?, finished_at=? WHERE id=?",
                ("process restarted before the job finished; re-run the import (idempotent)", events.utcnow(), job_id),
            )
            events.append(conn, "JOB_FAILED", {"job_id": job_id, "error_code": "INTERRUPTED"})
        return len(ids)

    return writer.run(_tx)


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["params"] = json.loads(job["params"])
    counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM job_items WHERE job_id = ? GROUP BY status", (job_id,)
    ).fetchall()
    job["items_by_status"] = {r["status"]: r["n"] for r in counts}
    job["progress"] = (job["done"] + job["failed"]) / job["total"] if job["total"] else 0.0
    return job


@dataclass
class JobContext:
    job_id: str
    writer: Writer

    def set_total(self, total: int) -> None:
        self.writer.run(lambda c: c.execute("UPDATE jobs SET total = ? WHERE id = ?", (total, self.job_id)))

    def record_item(
        self,
        idx: int,
        source_ref: str,
        status: str,
        artifact_id: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
        extra_tx: Callable[[sqlite3.Connection], str | None] | None = None,
    ) -> None:
        """Persist one unit result (and optional state change) in a single transaction.

        `extra_tx` runs first in the same transaction; if it returns a string, that becomes the
        item status (e.g. "duplicate").
        """
        ok = status not in {"failed", "quarantined"}

        def _tx(conn: sqlite3.Connection) -> None:
            final_status = status
            if extra_tx is not None:
                final_status = extra_tx(conn) or status
            conn.execute(
                "INSERT OR REPLACE INTO job_items (job_id, idx, source_ref, artifact_id, status, error_code, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.job_id, idx, source_ref, artifact_id, final_status, error_code, detail),
            )
            col = "done" if ok else "failed"
            conn.execute(f"UPDATE jobs SET {col} = {col} + 1 WHERE id = ?", (self.job_id,))

        self.writer.run(_tx)


Handler = Callable[[JobContext], None]


class JobRunner:
    """Runs handlers on a bounded thread pool (I/O-bound work: import, file moves).

    CPU-heavy model work should use its own process pool inside the handler.
    """

    def __init__(self, writer: Writer, max_workers: int = 2) -> None:
        self.writer = writer
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hydra-job")

    def submit(self, job_id: str, handler: Handler) -> Future[None]:
        return self.pool.submit(self._run, job_id, handler)

    def _run(self, job_id: str, handler: Handler) -> None:
        def _start(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (events.utcnow(), job_id))

        self.writer.run(_start)
        try:
            handler(JobContext(job_id, self.writer))
        except Exception as exc:  # noqa: BLE001 — job-level failure is data, not a crash
            code, msg = type(exc).__name__, "".join(traceback.format_exception_only(exc)).strip()

            def _fail(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "UPDATE jobs SET status='failed', error_code=?, error=?, finished_at=? WHERE id=?",
                    (code, msg[:2000], events.utcnow(), job_id),
                )
                events.append(conn, "JOB_FAILED", {"job_id": job_id, "error_code": code})

            self.writer.run(_fail)
            return

        def _done(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id=?", (events.utcnow(), job_id))
            row = conn.execute("SELECT total, done, failed FROM jobs WHERE id=?", (job_id,)).fetchone()
            events.append(conn, "JOB_DONE", {"job_id": job_id, **dict(row)})

        self.writer.run(_done)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=False)
