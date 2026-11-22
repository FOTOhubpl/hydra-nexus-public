"""Per-run response cache for the read API (API-PERF, 2026-11-21).

Analyse runs are immutable once their `runs` row exists (CORE writes runs, pairs, campaigns and members in one
transaction with `finished_at` set). What can still change for a run: `assign` runs scored against it (new
material joins its scope) and analyst reviews. So:

- heavy per-run aggregates are computed once and stored under `<state>/work/cache/api/<run_id>/` (gzip JSON) and
  in a small in-process LRU; the key is (endpoint, params, CACHE_VERSION, run fingerprint);
- the run fingerprint covers `finished_at`, the row counts of pairs / campaigns / members, the assign runs with
  their assignment counts and the batch runs (`assign_batch`, which rewrite assignment details in place), so new
  material or a re-written run gives a new key; a run without `finished_at` is never cached;
- review state, jobs, exports and audit counts are never cached: the endpoints overlay them live.

`HYDRA_API_CACHE=0` disables the cache (every request computes). `invalidate(settings, run_id)` drops one run.
Nothing here writes to the database.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import orjson
from fastapi.responses import Response

log = logging.getLogger("hydra.api_cache")

CACHE_VERSION = "1"
MEM_ITEMS = 32
_MEM: OrderedDict[str, Any] = OrderedDict()
_MEM_LOCK = threading.Lock()
_KEY_LOCKS: dict[str, threading.Lock] = {}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def enabled() -> bool:
    return os.environ.get("HYDRA_API_CACHE", "1") != "0"


def cache_root(settings: Any) -> Path:
    return Path(settings.work_dir) / "cache" / "api"


def run_dir(settings: Any, run_id: str) -> Path:
    return cache_root(settings) / _SAFE.sub("_", run_id)


def run_fingerprint(conn: sqlite3.Connection, run_id: str | None, *, base_only: bool = False) -> str | None:
    """Stable while the run's stored results and its assign runs are unchanged; None = do not cache.

    `base_only` (DATAFLOW, ADR-0009): only the immutable analyse run counts. Views that overlay new material from
    the materialised delta layer (hydra.dataflow) use it, so an assignment never invalidates their heavy part."""
    if not run_id:
        return None
    row = conn.execute("SELECT kind, finished_at FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None or not row[1]:
        return None
    one = lambda sql, args: conn.execute(sql, args).fetchone()  # noqa: E731
    parts: list[Any] = [run_id, row[0], row[1],
                        one("SELECT COUNT(*) FROM pairs WHERE run_id=?", (run_id,))[0],
                        one("SELECT COUNT(*) FROM campaigns WHERE run_id=?", (run_id,))[0],
                        one("SELECT COUNT(*) FROM campaign_members WHERE run_id=?", (run_id,))[0]]
    if base_only:
        return hashlib.sha1(json.dumps([*parts, "base"], default=str).encode()).hexdigest()[:16]
    from hydra.dataflow import assign_runs

    # CR-E2E-5: assign runs against the run or any run it revises (an analyst review never detaches them)
    assign = sorted(assign_runs(conn, run_id))
    runs = [run_id, *assign]
    a = one(f"SELECT COUNT(*), MAX(created_at) FROM assignments WHERE run_id IN ({','.join('?' * len(runs))})", runs)
    # variants-pipe batch runs (hydra.batch) rewrite the detail of those assignment rows in place (new_pattern_id,
    # duplicate_of) without changing their count or created_at: every batch run against this base is in the key
    batches = sorted(assign_runs(conn, run_id, "assign_batch"))
    parts += [assign, a[0], a[1], batches]
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def _key_lock(key: str) -> threading.Lock:
    with _MEM_LOCK:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = _KEY_LOCKS[key] = threading.Lock()
        return lock


def _mem_get(key: str) -> Any:
    with _MEM_LOCK:
        if key in _MEM:
            _MEM.move_to_end(key)
            return _MEM[key]
    return None


def _mem_put(key: str, value: Any) -> None:
    with _MEM_LOCK:
        _MEM[key] = value
        _MEM.move_to_end(key)
        while len(_MEM) > MEM_ITEMS:
            _MEM.popitem(last=False)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with gzip.open(tmp, "wb", compresslevel=3) as fh:
        fh.write(orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS))
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    with gzip.open(path, "rb") as fh:
        return orjson.loads(fh.read())


def cached(settings: Any, conn: sqlite3.Connection, endpoint: str, run_id: str | None, params: dict[str, Any],
           compute: Callable[[], Any], *, wrap: Callable[[Any], Any] | None = None,
           info: dict[str, Any] | None = None, base_only: bool = False) -> tuple[Any, bool]:
    """(value, hit). `compute()` must return JSON-serialisable data; `wrap` turns it (fresh or loaded from disk)
    into the in-memory object (e.g. with indexes). Values must be treated as read-only by callers. `info["fp"]`
    receives the run fingerprint (None = not cacheable) for response-level memo keys."""
    wrap = wrap or (lambda v: v)
    fp = run_fingerprint(conn, run_id, base_only=base_only) if enabled() else None
    if info is not None:
        info["fp"] = fp
    if fp is None:
        return wrap(compute()), False
    digest = hashlib.sha1(json.dumps([endpoint, params, CACHE_VERSION, fp], sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]
    path = run_dir(settings, run_id or "") / f"{_SAFE.sub('_', endpoint)}-{digest}.json.gz"
    key = str(path)
    if disk_only() and path.is_file():  # warming an older run: the file is all we need
        return None, True
    hit = _mem_get(key)
    if hit is not None:
        return hit, True
    with _key_lock(key):  # one computation per key even when the UI fires several requests at once
        hit = _mem_get(key)
        if hit is not None:
            return hit, True
        if path.is_file():
            try:
                value = wrap(_read(path))
                _mem_put(key, value)
                return value, True
            except (OSError, ValueError, EOFError) as exc:  # corrupt / partial file: recompute
                log.warning("api cache: unreadable %s (%s), recomputing", path.name, exc)
        t0 = time.perf_counter()
        raw = compute()
        try:
            _write(path, raw)
        except OSError as exc:  # read-only or full disk: serve without persisting
            log.warning("api cache: cannot write %s (%s)", path, exc)
        value = wrap(raw)
        if not disk_only():
            _mem_put(key, value)
        log.info("api cache: %s %s computed in %.2fs", endpoint, run_id, time.perf_counter() - t0)
        return value, False


def invalidate(settings: Any, run_id: str | None = None) -> None:
    """Drop the cache of one run (or of every run)."""
    target = run_dir(settings, run_id) if run_id else cache_root(settings)
    prefix = str(target)
    with _MEM_LOCK:
        for k in [k for k in _MEM if k.startswith(prefix)]:
            del _MEM[k]
        _RESP.clear()
    shutil.rmtree(target, ignore_errors=True)


def clear_memory() -> None:
    with _MEM_LOCK:
        _MEM.clear()
        _RESP.clear()


RESP_MAX_BYTES = 256 * 1024 * 1024
_RESP: OrderedDict[str, list[Any]] = OrderedDict()  # key -> [raw bytes, gzip bytes | None]


def reviews_digest(reviews: dict[Any, str]) -> str:
    return hashlib.sha1(repr(sorted(reviews.items())).encode()).hexdigest()[:16]


def memo_response(request: Any, key: list[Any], build: Callable[[], Any], *, hit: bool | None = None) -> Response:
    """Serialised (and lazily gzip-compressed) response bytes kept in memory per key, so repeated requests skip
    building, encoding and compressing multi-MB payloads. The key must contain everything the payload depends on
    (run fingerprint, params, review digest). gzip is served directly when the client accepts it; GZipMiddleware
    leaves responses with a Content-Encoding alone."""
    k = hashlib.sha1(json.dumps(key, default=str, sort_keys=True).encode()).hexdigest()
    with _MEM_LOCK:
        ent = _RESP.get(k)
        if ent is not None:
            _RESP.move_to_end(k)
    memo_hit = ent is not None
    if ent is None:
        ent = [orjson.dumps(build(), option=orjson.OPT_NON_STR_KEYS), None]
    accept = (request.headers.get("accept-encoding") or "") if hasattr(request, "headers") else ""
    headers = {"Vary": "Accept-Encoding"}
    if hit is not None:
        headers["X-Hydra-Cache"] = "hit" if (hit or memo_hit) else "miss"
    body = ent[0]
    if "gzip" in accept and len(ent[0]) >= 2048:
        if ent[1] is None:
            ent[1] = gzip.compress(ent[0], compresslevel=5)
        body = ent[1]
        headers["Content-Encoding"] = "gzip"
    if enabled():
        with _MEM_LOCK:
            _RESP[k] = ent
            _RESP.move_to_end(k)
            total = sum(len(v[0]) + len(v[1] or b"") for v in _RESP.values())
            while total > RESP_MAX_BYTES and len(_RESP) > 1:
                _, old = _RESP.popitem(last=False)
                total -= len(old[0]) + len(old[1] or b"")
    return Response(body, media_type="application/json", headers=headers)


def json_response(value: Any, *, hit: bool | None = None, status_code: int = 200) -> Response:
    """orjson-encoded response; skips FastAPI's jsonable_encoder walk, which dominates on multi-MB payloads."""
    headers = {} if hit is None else {"X-Hydra-Cache": "hit" if hit else "miss"}
    return Response(orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS), status_code=status_code,
                    media_type="application/json", headers=headers)


# --------------------------------------------------------------------------------------- image entropy
# Artifact ids are sha256 of the bytes, so an entropy value is valid forever and for every state: one JSON map
# per cache root, loaded once, appended on change.

_ENTROPY_LOCK = threading.Lock()
_ENTROPY_FILE: dict[str, dict[str, float]] = {}


def _entropy_path(settings_or_root: Any) -> Path:
    root = settings_or_root if isinstance(settings_or_root, Path) else cache_root(settings_or_root)
    return root / "image_entropy.json"


def entropy_map(root: Path) -> dict[str, float]:
    key = str(root)
    with _ENTROPY_LOCK:
        if key not in _ENTROPY_FILE:
            try:
                _ENTROPY_FILE[key] = {k: float(v) for k, v in orjson.loads(_entropy_path(root).read_bytes()).items()}
            except (OSError, ValueError):
                _ENTROPY_FILE[key] = {}
        return _ENTROPY_FILE[key]


def entropies(root: Path | None, store: Any, artifact_ids: list[str], fn: Callable[[Path], float],
              memo: dict[str, float]) -> dict[str, float]:
    """Entropy per artifact: in-process memo, then the on-disk map, then computed in a small thread pool (PIL
    decodes without the GIL). New values are persisted when `root` is given."""
    out = {a: memo[a] for a in artifact_ids if a in memo}
    disk = entropy_map(root) if root is not None and enabled() else {}
    for a in artifact_ids:
        if a not in out and a in disk:
            out[a] = memo[a] = disk[a]
    missing = sorted({a for a in artifact_ids if a not in out})
    if not missing:
        return out

    def one(aid: str) -> tuple[str, float]:
        try:
            return aid, fn(store.path_for(aid))
        except ValueError:
            return aid, 0.0

    workers = max(1, min(8, (os.cpu_count() or 2), len(missing)))
    if workers == 1:
        results = [one(a) for a in missing]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(one, missing))
    for aid, v in results:
        out[aid] = memo[aid] = v
    if root is not None and enabled():
        with _ENTROPY_LOCK:
            disk.update(dict(results))
            try:
                root.mkdir(parents=True, exist_ok=True)
                tmp = _entropy_path(root).with_suffix(f".{os.getpid()}.tmp")
                tmp.write_bytes(orjson.dumps(disk))
                os.replace(tmp, _entropy_path(root))
            except OSError as exc:
                log.warning("api cache: cannot persist image entropy (%s)", exc)
    return out


# --------------------------------------------------------------------------------------- warm-up

_WARM_LOCK = threading.Lock()
_LOCAL = threading.local()  # .disk_only: warming fills the disk cache without keeping values in memory


def disk_only() -> bool:
    return bool(getattr(_LOCAL, "disk_only", False))


def warm(app_state: Any, run_ids: list[str] | None = None, *, all_runs: bool = False,
         disk_only: bool = False) -> dict[str, float]:
    """Compute the cached views of the given runs (default: the newest analyse run of every corpus, kept in
    memory) and, with `all_runs`, then every other finished analyse run newest first (disk only, so memory stays
    small). Returns seconds per (run, view). Used at API start, after analyse jobs / applied reviews and at the
    end of `hydra.pipeline.analyse` (`precompute_run`)."""
    from hydra import api_graph, api_task, api_visual

    timings: dict[str, float] = {}
    with _WARM_LOCK:
        with app_state.reader() as conn:
            hot = latest_runs_per_corpus(conn) if run_ids is None else list(run_ids)
            rest = [r[0] for r in conn.execute("SELECT id FROM runs WHERE kind='analyse' AND finished_at IS NOT"
                                               " NULL ORDER BY created_at DESC") if r[0] not in hot] \
                if all_runs else []
            for run in hot + rest:
                _LOCAL.disk_only = disk_only or run in rest
                try:
                    for name, fn in (("graph", api_graph.warm_run), ("flow", api_visual.warm_run),
                                     ("dashboard", api_task.warm_run)):
                        t0 = time.perf_counter()
                        try:
                            fn(app_state, conn, run)
                        except Exception as exc:  # noqa: BLE001 — warming is best effort, requests still compute
                            log.warning("api cache: warming %s for %s failed: %s", name, run, exc)
                        timings[f"{run}:{name}"] = round(time.perf_counter() - t0, 3)
                finally:
                    _LOCAL.disk_only = False
    return timings


def precompute_run(settings: Any, run_id: str) -> dict[str, float] | None:
    """Fill the disk cache of a freshly written run from any process (CLI analyse, eval, API job), so the first
    UI request never computes. `HYDRA_API_PRECOMPUTE=0` or `HYDRA_API_CACHE=0` turns it off. Best effort."""
    if not enabled() or os.environ.get("HYDRA_API_PRECOMPUTE", "1") == "0":
        return None
    import contextlib
    from types import SimpleNamespace

    from hydra.db import connect
    from hydra.storage import ContentStore

    # writer=None (merge-batch4): DATAFLOW views read the aggregates `pipeline.analyse` built just before; outside
    # the API process there is no Writer to build missing ones, so `dataflow.ensure` falls back to the plain view
    shim = SimpleNamespace(settings=settings, store=ContentStore(settings.cas_dir), writer=None,
                           reader=lambda: contextlib.closing(connect(settings.db_path, readonly=True)))
    try:
        return warm(shim, [run_id], disk_only=True)
    except Exception as exc:  # noqa: BLE001 — never fail an analysis because of a view cache
        log.warning("api cache: precompute of %s failed: %s", run_id, exc)
        return None


def latest_runs_per_corpus(conn: sqlite3.Connection, limit: int = 6) -> list[str]:
    from hydra.api_task import _json, run_corpus

    seen: dict[str, str] = {}
    for r in conn.execute("SELECT id, params FROM runs WHERE kind='analyse' AND finished_at IS NOT NULL"
                          " ORDER BY created_at DESC"):
        seen.setdefault(run_corpus(_json(r["params"], {}) or {}, conn), r["id"])
    return list(seen.values())[:limit]


def warm_async(app_state: Any, run_ids: list[str] | None = None, delay_s: float = 0.0, *,
               all_runs: bool = False) -> threading.Thread | None:
    if not enabled() or os.environ.get("HYDRA_API_WARM", "1") == "0":
        return None

    def go() -> None:
        if delay_s:
            time.sleep(delay_s)
        try:
            t = warm(app_state, run_ids, all_runs=all_runs)
            log.info("api cache: warmed %s", t)
        except Exception as exc:  # noqa: BLE001 — e.g. state closed while warming
            log.warning("api cache: warm-up stopped: %s", exc)

    th = threading.Thread(target=go, name="hydra-api-warm", daemon=True)
    th.start()
    return th
