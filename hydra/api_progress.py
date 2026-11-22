"""Live activity of one analyse job for the /analyse page (IMPORT-PROGRESS, 2026-11-22).

`GET /v1/analyse/{job_id}/progress` returns one snapshot; `GET /v1/analyse/{job_id}/progress/stream` pushes the same
snapshot as Server-Sent Events (`event: progress`) whenever it changes (checked every 0.25 s, so <= 4 messages per
second), a comment heartbeat every 10 s, and ends after the terminal snapshot. The UI falls back to polling the JSON
endpoint when EventSource fails.

Sources, all read only (nothing here is written to the database):
- the import side channel `hydra.import_progress` (files listed / processed, bytes, rejected, current file);
- the pipeline's own progress file `hydra.progress.run_progress` (stage, incidents, pairs, self-assignment); for a
  single item the pipeline runs its own `analyse_input` job, found by the input reference the import sink recorded;
- the `jobs` row (status, error).

Overall percent: a weighted sum of stages; the weights are measured per-stage seconds (corpus: JURY_READINESS.md §3,
budget 8, the N row nearest the run's incident count; single item: docs/SPEC_ERRATA.md E-PROG-2). A stage without a
determinate counter contributes 0 until it ends (never estimated movement). The percent never decreases and is 100
only when the job is done.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sqlite3
import time
from typing import Any

import orjson
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from hydra import import_progress
from hydra.progress import run_progress

router = APIRouter(prefix="/v1", tags=["analyse"])

TICK_S = 0.25
HEARTBEAT_S = 10.0

CORPUS_STAGES = [("import", "Import"), ("genes", "Ekstrakcja genów"), ("scoring", "Rzadkość i ocena par"),
                 ("clustering", "Grupowanie"), ("calibration", "Kalibracja"), ("self_assign", "Samoprzypisanie"),
                 ("persist", "Zapis"), ("done", "Gotowe")]
INPUT_STAGES = [("import", "Import"), ("genes", "Ekstrakcja genów"), ("enrich", "Wzbogacanie"),
                ("scoring", "Kandydaci i ocena"), ("decision", "Decyzja"), ("done", "Gotowe")]

# Measured per-stage seconds, JURY_READINESS.md §3 (the Linux server, HYDRA_CPU_BUDGET=8, synthetic jury corpus):
# import / genes + load_genomes / rarity+scoring+clustering / conformal / self_assignment / persist + aggregates.
# Clustering is measured together with scoring, so it carries no weight of its own.
CORPUS_WEIGHTS = {
    50: {"import": 0.7, "genes": 59.3, "scoring": 5.4, "clustering": 0.0, "calibration": 9.1, "self_assign": 2.5,
         "persist": 2.4},
    200: {"import": 0.9, "genes": 165.5, "scoring": 12.1, "clustering": 0.0, "calibration": 27.5,
          "self_assign": 18.6, "persist": 24.7},
    1000: {"import": 5.2, "genes": 834.1, "scoring": 170.0, "clustering": 0.0, "calibration": 157.0,
           "self_assign": 512.0, "persist": 511.0},
}
# Single item, measured with `scripts/bench_import_progress.py --stages` (laptop M4, empty corpus, cold models,
# 2026-11-22): import 0.013 s, genes 15.5 s; enrich / candidates / scoring / decision were below the 5 ms sampling
# step, so they carry no weight (docs/SPEC_ERRATA.md E-PROG-2).
INPUT_WEIGHTS = {"import": 0.013, "genes": 15.5, "enrich": 0.0, "scoring": 0.0, "decision": 0.0}


def corpus_weights(n: int | None) -> tuple[dict[str, float], int]:
    """Weights of the measured row nearest (log scale) to `n` incidents; 200 when unknown."""
    if not n:
        return CORPUS_WEIGHTS[200], 200
    key = min(CORPUS_WEIGHTS, key=lambda k: abs(math.log(k) - math.log(max(1, n))))
    return CORPUS_WEIGHTS[key], key


def _frac(done: Any, total: Any) -> float | None:
    try:
        d, t = float(done), float(total)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, d / t)) if t > 0 else None


def _self_assign_total(n: int) -> int:
    try:
        from hydra.pipeline import self_assign_cap

        cap = self_assign_cap()
    except Exception:  # noqa: BLE001 — display only
        cap = None
    return min(cap, n) if cap else n


def _corpus_stage(core: dict[str, Any] | None, imp: dict[str, Any] | None) -> tuple[str, float | None, dict]:
    """(stage key, fraction or None, counters) of a corpus job from the pipeline's progress file."""
    st = (core or {}).get("stage") or "import"
    n = int((core or {}).get("incidents_total") or 0)
    c: dict[str, Any] = {}
    if st == "import":
        return "import", (imp or {}).get("percent") / 100 if (imp or {}).get("percent") is not None else None, c
    if st == "genes":
        if core.get("extract_total"):  # per extractor result (feat/progress-pipeline instrumentation), when present
            c = {"done": core.get("extract_done", 0), "total": core["extract_total"], "unit": "wyników ekstraktorów",
                 "item": core.get("extract_current")}
            return "genes", _frac(c["done"], c["total"]), c
        c = {"done": core.get("incidents_done", 0), "total": n, "unit": "incydentów"}
        return "genes", _frac(c["done"], n), c
    if st in ("rarity", "candidates"):
        return "scoring", None, {"sub": "rzadkość" if st == "rarity" else "kandydaci"}
    if st == "scoring":
        c = {"done": core.get("pairs_scored", 0), "total": core.get("pairs_total", 0), "unit": "par"}
        return "scoring", _frac(c["done"], c["total"]), c
    if st == "clustering":
        return "clustering", None, c
    if st == "assign":
        k = core.get("self_assigned")
        if k is None:
            return "calibration", None, c
        total = int(core.get("self_assign_total") or 0) or _self_assign_total(n)
        c = {"done": k, "total": total, "unit": "incydentów"}
        if total and k >= total:
            return "persist", None, {}
        return "self_assign", _frac(k, total), c
    if st == "done":
        return "done", 1.0, c
    return "import", None, c


def _input_stage(core: dict[str, Any] | None, imp: dict[str, Any] | None) -> tuple[str, float | None, dict]:
    st = (core or {}).get("stage")
    if not st or st == "import":
        return "import", (imp or {}).get("percent") / 100 if (imp or {}).get("percent") is not None else None, {}
    if st in ("candidates", "scoring"):
        c = {"done": core.get("pairs_scored", 0), "total": core.get("pairs_total", 0), "unit": "par"}
        return "scoring", _frac(c["done"], c["total"]) if st == "scoring" else None, c
    if st == "genes":  # one extractor result at a time (pipeline `_on_extractor` -> Progress.extractor_done)
        ex = list((core or {}).get("extractors") or {})
        total = max(len(ex), _extractor_count())
        c = {"done": len(ex), "total": total, "unit": "ekstraktorów", "item": ex[-1] if ex else None}
        return "genes", _frac(len(ex), total), c
    if st in ("enrich", "decision", "done"):
        return st, 1.0 if st == "done" else None, {}
    return "import", None, {}


def _extractor_count() -> int:
    try:
        from hydra.genome import base

        return len(base.REGISTRY)
    except Exception:  # noqa: BLE001 — display only
        return 0


def _inner_job(conn: sqlite3.Connection, job: dict[str, Any], ref: str | None) -> str | None:
    """The pipeline's own `analyse_input` job of a single-item API job (created after it started, same input)."""
    if not ref or not job.get("started_at"):
        return None
    row = conn.execute("SELECT id FROM jobs WHERE kind='analyse_input' AND created_at >= ? AND"
                       " json_extract(params, '$.payload') = ? ORDER BY created_at LIMIT 1",
                       (job["started_at"], ref)).fetchone()
    return row[0] if row else None


def snapshot(st: Any, job_id: str) -> dict[str, Any] | None:
    with st.reader() as conn:
        row = conn.execute("SELECT id, kind, status, params, total, done, error_code, error, created_at, started_at,"
                           " finished_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["kind"] != "analyse":
            return None
        job = dict(row)
        params = json.loads(job.pop("params") or "{}")
        imp = import_progress.read(st.settings, job_id)
        mode = str(params.get("input_kind") or params.get("mode") or "")
        corpus = mode == "corpus"
        core_id = job_id if corpus else _inner_job(conn, job, (imp or {}).get("input_ref"))
    core = run_progress(core_id, st.settings) if core_id else None
    if core is not None:
        core.pop("job", None)
    status = job["status"]
    stages_def = CORPUS_STAGES if corpus else INPUT_STAGES
    if status == "done":
        key, frac, counters = "done", 1.0, {}
    else:
        key, frac, counters = (_corpus_stage if corpus else _input_stage)(core, imp)
    if imp is not None:
        imp.pop("input_ref", None)
        if key != "import" and imp.get("phase") not in ("done", "failed"):
            imp = {**imp, "phase": "done", "percent": 100, "current": None}  # the pipeline has moved past import
        if status == "failed" and imp.get("phase") != "done":
            imp = {**imp, "phase": "failed", "error": imp.get("error") or job.get("error")}
    if corpus:
        n = (core or {}).get("incidents_total") or (imp or {}).get("incidents_total")
        weights, weights_n = corpus_weights(int(n) if n else None)
        weights_src = f"JURY_READINESS.md §3, N={weights_n}, budżet 8"
    else:
        weights, weights_src = INPUT_WEIGHTS, "SPEC_ERRATA E-PROG-2 (pojedynczy materiał)"
    keys = [k for k, _ in stages_def]
    idx = keys.index(key) if key in keys else 0
    total_w = sum(weights.values()) or 1.0
    overall = sum(weights.get(k, 0.0) for k in keys[:idx]) + weights.get(key, 0.0) * (frac or 0.0)
    pct = 100.0 if status == "done" else min(99.0, overall * 100 / total_w)
    stages = []
    for i, (k, label) in enumerate(stages_def):
        state = "done" if i < idx or status == "done" else ("running" if i == idx else "waiting")
        if status == "failed" and i == idx:
            state = "failed"
        stages.append({"key": k, "label": label, "state": state,
                       "weight": round(weights.get(k, 0.0) / total_w, 4) if k != "done" else 0.0,
                       "percent": (100.0 if state == "done" else round(frac * 100, 1) if i == idx and
                                   frac is not None else None)})
    return {
        "job_id": job_id, "status": status, "mode": "corpus" if corpus else "single", "source": params.get("source"),
        "stage": key, "stage_label": dict(stages_def).get(key, key), "stage_percent": None if frac is None else
        round(frac * 100, 1), "counters": counters, "stages": stages, "overall_percent": round(pct, 1),
        "weights_source": weights_src, "import": imp,
        "core": None if core is None else {k: core.get(k) for k in (
            "stage", "incidents_total", "incidents_done", "genes", "genes_by_family", "pairs_total", "pairs_scored",
            "campaigns", "groups", "self_assigned", "updated_at", "incident_id", "decision")},
        "error_code": job.get("error_code"), "error": job.get("error") if status == "failed" else None,
        "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
    }


def _st(request: Request) -> Any:
    return request.app.state.hydra


@router.get("/analyse/{job_id}/progress")
def analyse_progress(request: Request, job_id: str) -> dict[str, Any]:
    snap = snapshot(_st(request), job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="analyse job not found")
    return _redact(snap)


def _redact(snap: dict[str, Any]) -> dict[str, Any]:
    from hydra.api_task import safe_ref

    if snap.get("error"):
        snap["error"] = safe_ref(snap["error"])
    imp = snap.get("import")
    if imp and imp.get("current"):
        imp["current"] = safe_ref(imp["current"])
    if imp and imp.get("error"):
        imp["error"] = safe_ref(imp["error"])
    return snap


def _signature(snap: dict[str, Any]) -> bytes:
    return orjson.dumps({k: v for k, v in snap.items() if k not in ("finished_at",)},
                        option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS)


@router.get("/analyse/{job_id}/progress/stream")
async def analyse_progress_stream(request: Request, job_id: str) -> StreamingResponse:
    from hydra.api_stream import _origin_ok

    if not _origin_ok(request):
        raise HTTPException(status_code=403, detail="Odrzucono strumień z innego pochodzenia (Origin).")
    st = _st(request)
    first = await asyncio.to_thread(snapshot, st, job_id)
    if first is None:
        raise HTTPException(status_code=404, detail="analyse job not found")

    async def body():
        last_sig = b""
        last_send = time.monotonic()
        snap: dict[str, Any] | None = first
        seq = 0
        while True:
            if snap is not None:
                sig = _signature(snap)
                if sig != last_sig:
                    last_sig, seq = sig, seq + 1
                    last_send = time.monotonic()
                    yield (f"id: {seq}\nevent: progress\ndata: ".encode()
                           + orjson.dumps(_redact(snap), option=orjson.OPT_NON_STR_KEYS) + b"\n\n")
                if snap["status"] in ("done", "failed"):
                    return
            if time.monotonic() - last_send >= HEARTBEAT_S:
                last_send = time.monotonic()
                yield b": heartbeat\n\n"
            if await request.is_disconnected():
                return
            await asyncio.sleep(TICK_S)
            with contextlib.suppress(Exception):  # e.g. state replaced under us: keep the previous snapshot
                snap = await asyncio.to_thread(snapshot, st, job_id)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
