"""Import-phase progress of an analyse job (IMPORT-PROGRESS, 2026-11-22), outside the frozen scoring paths.

The pipeline's own `hydra.progress.Progress` reports only `stage=import` until every incident is imported (and the
pipeline is frozen, eval/manifest.py SCORING_PATHS). This module is the side channel: the API job handler installs an
`ImportProgress` sink in a context variable, `hydra.jury_report.jury_analyse` pre-counts the files of the discovered
incident folders (total known after listing), and the adapter (`nask_folder.import_incident`) reports each stored
file, its bytes and the rejected files. Read by `GET /v1/analyse/{job_id}/import`.

Storage: `<state>/progress/<job_id>.import.json`, atomic replace, never the database (no write on the single Writer,
so the import is not slowed by progress rows). Flushes are throttled to `min_interval` (default 0.25 s, so <= 4 per
second); phase changes and the final state are written immediately. Counters only grow; `percent` never decreases
and is 100 only when every listed file was processed. Read only for the analysis: nothing here changes an artifact,
gene, score or decision.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hydra import events
from hydra.config import Settings

CURRENT_MAX = 80  # characters of the current file name shown in the UI (tail kept: the file name matters)
PHASES = ("listing", "importing", "done", "failed")

_SINK: contextvars.ContextVar[ImportProgress | None] = contextvars.ContextVar("hydra_import_progress", default=None)


def _progress_path(settings: Settings, job_id: str) -> Path:
    assert settings.state_dir is not None
    d = settings.state_dir / "progress"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job_id}.import.json"


def _short(name: str) -> str:
    name = name.replace("\\", "/")
    return name if len(name) <= CURRENT_MAX else "…" + name[-(CURRENT_MAX - 1):]


class ImportProgress:
    """Thread-safe counters for one job. All methods are cheap (a lock, a few adds, a throttled file write)."""

    def __init__(self, settings: Settings, job_id: str, *, min_interval: float = 0.25,
                 incidents_total: int | None = None, input_ref: str | None = None) -> None:
        self.path = _progress_path(settings, job_id)
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self.writes = 0
        self._expected: dict[str, tuple[int, int]] = {}  # resolved incident folder -> (files, bytes) from listing
        self._base_files = 0   # files of finished incidents
        self._base_bytes = 0
        self._cur: dict[str, int] = {}
        self._cur_key = ""
        self._begun = 0
        self.state: dict[str, Any] = {
            "job_id": job_id, "phase": "listing", "files_total": None, "files_done": 0, "bytes_total": None,
            "bytes_done": 0, "rejected": 0, "incidents_total": incidents_total, "incidents_done": 0, "current": None,
            "percent": None, "started_at": events.utcnow(), "updated_at": None, "error": None,
            "input_ref": input_ref,  # internal: links the pipeline's own analyse_input job (never sent to the UI)
        }
        self._flush(force=True)

    # ------------------------------------------------------------------ listing
    def expect(self, folders: dict[Path, tuple[int, int]], incidents: int | None = None) -> None:
        """Totals after listing: files and bytes per incident folder (what `import_incident` will walk).
        `incidents`: incidents to import when some are single files (imported from a scratch copy, counted then)."""
        with self._lock:
            self._expected = {str(Path(f).resolve()): v for f, v in folders.items()}
            self.state["files_total"] = sum(n for n, _ in self._expected.values())
            self.state["bytes_total"] = sum(b for _, b in self._expected.values())
            self.state["incidents_total"] = max(len(self._expected), incidents or 0)
            self.state["phase"] = "importing"
            self._percent()
            self._flush(force=True)

    # ------------------------------------------------------------------ per incident (adapter hooks)
    def begin_incident(self, folder: Path, files: int, size: int, rejected: int, rejected_files: int = 0) -> None:
        """`files`/`size`: the top-level files the adapter walked in `folder` (what the listing counted);
        `rejected`: every file or entry it refused there (empty, too large, hidden non-data, symlink, unreadable
        archive entry); `rejected_files`: how many of the walked files are among them (they count as processed)."""
        with self._lock:
            key = str(Path(folder).resolve())
            if key not in self._expected:  # not listed up front (single item, or a folder found late)
                self._expected[key] = (files, size)
                self.state["files_total"] = (self.state["files_total"] or 0) + files
                self.state["bytes_total"] = (self.state["bytes_total"] or 0) + size
            self._begun += 1
            if (self.state["incidents_total"] or 0) < self._begun:
                self.state["incidents_total"] = self._begun
            n, _ = self._expected[key]
            self._cur = {"files": min(n, rejected_files), "bytes": 0, "cap": n}
            self._cur_key = key
            self.state["rejected"] += rejected
            first = self.state["phase"] != "importing"
            self.state["phase"] = "importing"
            self._sync()
            self._flush(force=first)

    def file_done(self, name: str, size: int, *, top_level: bool = True) -> None:
        with self._lock:
            if not self._cur:
                return
            if top_level:
                self._cur["files"] = min(self._cur["cap"], self._cur["files"] + 1)
                self._cur["bytes"] += max(0, int(size))
            self.state["current"] = _short(name)
            self._sync()
            self._flush()

    def end_incident(self) -> None:
        with self._lock:
            if not self._cur:
                return
            n, size = self._expected.get(self._cur_key, (self._cur["cap"], self._cur["bytes"]))
            self._base_files += n
            self._base_bytes += size
            self._cur = {}
            self.state["incidents_done"] += 1
            self._sync()
            total = self.state["incidents_total"]
            if total is not None and self.state["incidents_done"] >= total and \
                    self.state["files_done"] >= (self.state["files_total"] or 0):
                self.state["phase"] = "done"
                self.state["current"] = None
                self._percent()
            self._flush(force=self.state["phase"] == "done")

    def finish(self, status: str = "done", error: str | None = None) -> None:
        """End of the job handler: `done` closes the import phase at 100 %; `failed` keeps the counters."""
        with self._lock:
            if status == "done":
                if self.state["phase"] != "done":
                    self.state["files_total"] = max(self.state["files_total"] or 0, self.state["files_done"])
                    self.state["files_done"] = self.state["files_total"]
                self.state.update(phase="done", current=None)
            elif self.state["phase"] != "done":
                self.state.update(phase="failed", error=(error or "")[:500] or None)
            self._percent()
            self._flush(force=True)

    # ------------------------------------------------------------------ internals
    def _sync(self) -> None:
        files = self._base_files + (self._cur.get("files", 0) if self._cur else 0)
        self.state["files_done"] = max(self.state["files_done"], files)
        size = self._base_bytes + (self._cur.get("bytes", 0) if self._cur else 0)
        self.state["bytes_done"] = max(self.state["bytes_done"], size)
        self._percent()

    def _percent(self) -> None:
        total = self.state["files_total"]
        if not total:
            if self.state["phase"] == "done":
                self.state["percent"] = 100
            return
        pct = min(100, int(self.state["files_done"] * 100 / total))
        if pct == 100 and self.state["phase"] != "done":
            pct = 99  # 100 only once the phase is closed
        self.state["percent"] = max(self.state["percent"] or 0, pct)

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < self.min_interval:
            return
        self._last = now
        self.state["updated_at"] = events.utcnow()
        tmp = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self.writes += 1
        except OSError:  # progress is best effort: a full disk must never fail the import itself
            pass


def current() -> ImportProgress | None:
    return _SINK.get()


@contextmanager
def tracking(settings: Settings, job_id: str, **kw: Any) -> Iterator[ImportProgress]:
    """Install a sink for the calling context (the job handler thread) and close it with the handler's outcome."""
    sink = ImportProgress(settings, job_id, **kw)
    token = _SINK.set(sink)
    try:
        yield sink
    except BaseException as exc:
        sink.finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    else:
        sink.finish("done")
    finally:
        _SINK.reset(token)


def read(settings: Settings, job_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_progress_path(settings, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
