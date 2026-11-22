"""Live progress of an analyse run, for the UI's one-click animation.

Stages: import → genes → rarity → candidates → scoring → clustering → assign (conformal calibration) → done.
State per job: stage, incidents done/total, genes per family so far, pairs scored, campaigns formed, timestamps.

Storage: a JSON file `<state>/progress/<job_id>.json` (atomic replace; read by `run_progress`) plus the generic
`jobs` row (status, total, done) through the single Writer. The jobs table has no progress column (CR-CORE-5 asks
for one); the file is the workaround. Updates are throttled: stage changes are written immediately, counters at
most once per `min_interval` seconds.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from hydra import events
from hydra.config import Settings
from hydra.db import Writer, connect

STAGES = ["import", "genes", "rarity", "candidates", "scoring", "clustering", "assign", "done"]
INPUT_STAGES = ["import", "genes", "enrich", "candidates", "scoring", "decision", "done"]  # analyse_input


def progress_dir(settings: Settings) -> Path:
    assert settings.state_dir is not None
    d = settings.state_dir / "progress"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Progress:
    def __init__(self, settings: Settings, writer: Writer, job_id: str, *, min_interval: float = 1.0,
                 run_id: str | None = None, corpus: str | None = None, stages: list[str] | None = None,
                 **extra: Any) -> None:
        self.stages = stages or STAGES
        self.settings, self.writer, self.job_id = settings, writer, job_id
        self.min_interval = min_interval
        self.path = progress_dir(settings) / f"{job_id}.json"
        self._last = 0.0
        self.state: dict[str, Any] = {
            "job_id": job_id, "run_id": run_id, "corpus": corpus, "stage": "import", "stage_index": 0,
            "stages": self.stages, "extractors": {}, **extra, "incidents_total": 0, "incidents_done": 0,
            "genes_by_family": {}, "genes": 0,
            "pairs_total": 0, "pairs_scored": 0, "campaigns": 0, "groups": 0, "started_at": events.utcnow(),
            "updated_at": events.utcnow(), "status": "running", "error": None,
        }
        self._families: Counter[str] = Counter()
        self._flush(force=True, job_status="running")

    # ----------------------------------------------------------------- updates
    def stage(self, name: str, **fields: Any) -> None:
        self.state.update(stage=name, stage_index=self.stages.index(name) if name in self.stages else -1, **fields)
        self._flush(force=True)

    def set(self, **fields: Any) -> None:
        self.state.update(fields)
        self._flush()

    def incident_done(self, genes: list | None = None) -> None:
        self.state["incidents_done"] += 1
        for g in genes or []:
            self._families[str(g.family)] += 1
        self.state["genes_by_family"] = dict(sorted(self._families.items()))
        self.state["genes"] = sum(self._families.values())
        self._flush(force=self.state["incidents_done"] >= self.state["incidents_total"])

    def extractor_done(self, name: str, genes: int, seconds: float, families: dict[str, int] | None = None) -> None:
        self.state["extractors"][name] = {"genes": genes, "seconds": round(seconds, 3)}
        for f, n in (families or {}).items():
            self._families[f] += n
        self.state["genes_by_family"] = dict(sorted(self._families.items()))
        self.state["genes"] = sum(self._families.values())
        self._flush()

    def finish(self, status: str = "done", error: str | None = None, **fields: Any) -> None:
        self.state.update(fields, status=status, error=error)
        if status == "done":
            self.state.update(stage="done", stage_index=self.stages.index("done"))
        self._flush(force=True, job_status="done" if status == "done" else "failed")

    # ----------------------------------------------------------------- io
    def _flush(self, force: bool = False, job_status: str | None = None) -> None:
        now = time.monotonic()
        if not force and now - self._last < self.min_interval:
            return
        self._last = now
        self.state["updated_at"] = events.utcnow()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, self.path)
        total, done = int(self.state["incidents_total"]), int(self.state["incidents_done"])

        def _tx(conn: sqlite3.Connection) -> None:
            if job_status == "running":
                conn.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                             (events.utcnow(), self.job_id))
            conn.execute("UPDATE jobs SET total=?, done=? WHERE id=?", (total, done, self.job_id))
            if job_status in ("done", "failed"):
                conn.execute("UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                             (job_status, self.state.get("error"), events.utcnow(), self.job_id))

        self.writer.run(_tx)


def run_progress(job_id: str, settings: Settings) -> dict[str, Any] | None:
    """Current progress of an analyse job (None if unknown). Cheap: one small file + one row."""
    p = progress_dir(settings) / f"{job_id}.json"
    state = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
    if settings.db_path.exists():
        with closing(connect(settings.db_path, readonly=True)) as conn:
            row = conn.execute("SELECT status, total, done, error FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is not None:
            state = state or {"job_id": job_id}
            state["job"] = dict(row)
    return state
