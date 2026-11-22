"""Task endpoints (contracts v2.0.0): runs, incidents, campaigns, graph, pairs, assignments, analyse, reviews.

Read side only reads the v2 tables written by CORE (`hydra.pipeline`) through `AppState.reader()`. The only
writes here are analyst reviews (with a hash-chained audit event in the same transaction) and analyse jobs.

Rules kept here:
- `incidents.label` is ground truth for evaluation only and is never returned.
- No run yet = empty lists with `run_id: null`, never invented rows.
- Missing families are reported as unknown (`missing_families`), not as negative evidence.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time as _time
import uuid
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from hydra import api_cache, events
from hydra.api_auth import acting_analyst
from hydra.contracts import CONTRACTS_VERSION, Family, ReviewAction
from hydra.contracts import Review as ReviewModel
from hydra.export_log import record_export
from hydra.export_safe import content_disposition, csv_row, safe_filename
from hydra.jobs import JobContext, create_job, get_job
from hydra.redact import redact_obj, redact_text

router = APIRouter(prefix="/v1", tags=["task"])

FAMILY_ORDER = [f.value for f in Family]
INFRA_KINDS = (
    "domain", "host", "url", "url_skeleton", "redirect_chain", "ip", "asn", "ns", "mx", "cert_san",
    "tracker_id", "tds_param", "favicon_mmh3", "registrar",
)
VISUAL_ROLES = ("ad_creative", "feed_screenshot", "landing_screenshot", "screen_recording", "ad_video")
ANALYSE_EVENT = "ANALYSE_DONE"


def _st(request: Request) -> Any:
    return request.app.state.hydra


def _json(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _fast_json(text: str | None) -> dict[str, Any]:
    """orjson for bulk pair/gene JSON (several times faster than json.loads on multi-KB documents)."""
    import orjson

    try:
        d = orjson.loads(text) if text else {}
    except orjson.JSONDecodeError:
        return {}
    return d if isinstance(d, dict) else {}


def _page(items: list[Any], total: int, limit: int, offset: int, run_id: str | None = None) -> dict[str, Any]:
    return {"items": items, "total": total, "limit": limit, "offset": offset, "run_id": run_id}


_HOME_RE = re.compile(r"(?:/Users|/home)/[^/\s]+/")


def safe_ref(value: Any) -> Any:
    """Local paths without the OS username (review #20): `/Users/<name>/x` → `~/x`; secrets in file names or
    URLs (tokens in a saved page's name) are redacted (CR-ASTRA-SEC-13)."""
    if not isinstance(value, str):
        return value
    return redact_text(_HOME_RE.sub("~/", value))


def _assignment_runs(conn: sqlite3.Connection, run: str | None) -> list[str]:
    """The analyse run itself plus every `assign` run scored against it (review #3: CORE writes
    assignments under `assign-*` run ids with params.base_run = the analyse run)."""
    from hydra.dataflow import assign_runs

    if not run:
        return []
    return [run, *assign_runs(conn, run)]  # CR-E2E-5: including assign runs against the runs it revises


def _latest_assignments(conn: sqlite3.Connection, run: str | None, *, incident_ids: list[str] | None = None,
                        decision: str | None = None) -> list[sqlite3.Row]:
    """Newest assignment per incident across the analyse run and its assign runs."""
    runs = _assignment_runs(conn, run)
    if not runs:
        return []
    where = [f"run_id IN ({','.join('?' * len(runs))})"]
    args: list[Any] = list(runs)
    if incident_ids is not None:
        if not incident_ids:
            return []
        where.append(f"incident_id IN ({','.join('?' * len(incident_ids))})")
        args += incident_ids
    rows = conn.execute(
        f"SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY incident_id ORDER BY created_at DESC, run_id DESC)"
        f" AS rn FROM assignments WHERE {' AND '.join(where)}) WHERE rn=1"
        + (" AND decision=?" if decision else "") + " ORDER BY created_at DESC, incident_id",
        [*args, *([decision] if decision else [])],
    ).fetchall()
    return rows


# --------------------------------------------------------------------------------------- runs


def resolve_run(conn: sqlite3.Connection, run_id: str | None = None, kind: str = "analyse") -> str | None:
    """Explicit run id (404 when no results are stored under it, CR-ASTRA-PIPE-302), else the newest finished run
    of `kind`, else the newest of any state."""
    if run_id:
        from hydra.campaign_facts import run_known

        if not run_known(conn, run_id):
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return run_id
    # Stub-scorer eval runs (params.backend contains "STUB") are development artefacts, never the default.
    row = conn.execute(
        "SELECT id FROM runs WHERE kind=? AND COALESCE(json_extract(params, '$.backend'), '') NOT LIKE '%STUB%'"
        " ORDER BY finished_at IS NULL, created_at DESC LIMIT 1", (kind,)
    ).fetchone()
    if row:
        return row["id"]
    if kind != "analyse":
        return None
    # Results written without a runs row (should not happen, but never hide data that exists).
    row = conn.execute(
        "SELECT run_id FROM campaigns UNION SELECT run_id FROM pairs ORDER BY 1 DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _run_row(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    run["params"] = _json(run["params"], {})
    run["metrics"] = _json(run["metrics"], {})
    return run


def _run_counts(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    one = lambda sql: conn.execute(sql, (run_id,)).fetchone()[0]  # noqa: E731
    return {
        "pairs": one("SELECT COUNT(*) FROM pairs WHERE run_id=?"),
        "campaigns": one("SELECT COUNT(*) FROM campaigns WHERE run_id=?"),
        "assignments": len(_latest_assignments(conn, run_id)),
        "members": one("SELECT COUNT(DISTINCT incident_id) FROM campaign_members WHERE run_id=?"),
    }


@router.get("/runs")
def list_runs(request: Request, kind: str | None = None, limit: int = Query(50, le=500),
              full: bool = Query(False, description="old shape: params + metrics inline for every run")) -> Any:
    """Run list. Default: a summary per run (id, kind, times, corpus, counts); full params + metrics of one run
    via GET /v1/runs/{run_id} (metrics are ~100-400 KB per analyse run). `full=true` keeps the old shape."""
    with _st(request).reader() as conn:
        where, args = ("WHERE kind=?", [kind]) if kind else ("", [])
        total = conn.execute(f"SELECT COUNT(*) FROM runs {where}", args).fetchone()[0]
        if full:
            rows = conn.execute(f"SELECT id FROM runs {where} ORDER BY created_at DESC LIMIT ?",
                                [*args, limit]).fetchall()
            return _page([_run_row(conn, r["id"]) for r in rows], total, limit, 0)
        rows = conn.execute(
            "SELECT id, kind, created_at, finished_at, params, json_extract(metrics, '$.incidents') AS incidents"
            f" FROM runs {where} ORDER BY created_at DESC LIMIT ?", [*args, limit]).fetchall()
        corpus_of = {r["id"]: run_corpus(_json(r["params"], {}) or {}, conn) for r in rows if r["kind"] == "analyse"}
        ids = [r["id"] for r in rows]
        counts: dict[str, dict[str, int]] = defaultdict(lambda: {"pairs": 0, "campaigns": 0, "members": 0})
        if ids:
            for table, col, expr in (("pairs", "pairs", "COUNT(*)"), ("campaigns", "campaigns", "COUNT(*)"),
                                     ("campaign_members", "members", "COUNT(DISTINCT incident_id)")):
                for r in conn.execute(f"SELECT run_id, {expr} FROM {table} WHERE run_id IN ({_in(ids)})"
                                      " GROUP BY run_id", ids):
                    counts[r[0]][col] = r[1]
    items = []
    for r in rows:
        params = _json(r["params"], {}) or {}
        items.append({"id": r["id"], "kind": r["kind"], "created_at": r["created_at"], "finished_at": r["finished_at"],
                      "corpus": corpus_of.get(r["id"]),
                      "revision_of": params.get("revision_of"), "base_run": params.get("base_run"),
                      "synthetic": bool(params.get("synthetic")),
                      "counts": {**counts[r["id"]], "incidents": r["incidents"]}})
    return _page(items, total, limit, 0)


@router.get("/runs/latest")
def latest_run(request: Request, kind: str = "analyse") -> dict[str, Any]:
    """Latest run of a kind (analyse | eval) with counts. `run: null` when nothing has run yet."""
    with _st(request).reader() as conn:
        run_id = resolve_run(conn, None, kind)
        incidents = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        if run_id is None:
            return {"run": None, "counts": {"incidents": incidents}, "contracts_version": CONTRACTS_VERSION}
        run = _run_row(conn, run_id) or {"id": run_id, "kind": kind, "params": {}, "metrics": {},
                                         "created_at": None, "finished_at": None}
        counts = {"incidents": incidents, **_run_counts(conn, run_id)}
    return {"run": run, "counts": counts, "contracts_version": CONTRACTS_VERSION}


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    """One run with params and metrics (the list endpoint returns summaries only)."""
    with _st(request).reader() as conn:
        run = _run_row(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        counts = _run_counts(conn, run_id)
        corpus = run_corpus(run["params"] or {}, conn)
    return {**run, "corpus": corpus, "counts": counts}


_AUDIT_CACHE: dict[str, dict[str, Any]] = {}
AUDIT_FULL_RECHECK_S = 300.0


def verify_chain_cached(conn: sqlite3.Connection, key: str) -> tuple[bool, int, int | None]:
    """Review #21: verify only events appended since the last poll; a full re-verify every 5 min still
    catches edits to old events. A broken chain stays reported as broken."""
    now = _time.monotonic()
    c = _AUDIT_CACHE.get(key)
    if c is None or now - c["full_at"] > AUDIT_FULL_RECHECK_S:
        ok, n, bad = events.verify_chain(conn)
        last = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        _AUDIT_CACHE[key] = {"ok": ok, "n": n, "bad": bad, "seq": last["seq"] if last else 0,
                             "hash": last["hash"] if last else events.GENESIS, "full_at": now}
        return ok, n, bad
    if not c["ok"]:
        return False, c["n"], c["bad"]
    prev, n = c["hash"], c["n"]
    for row in conn.execute("SELECT seq, ts, type, payload, prev_hash, hash FROM events WHERE seq>? ORDER BY seq",
                            (c["seq"],)):
        n += 1
        if row["prev_hash"] != prev or events._event_hash(prev, row["ts"], row["type"], row["payload"]) != row["hash"]:
            c.update({"ok": False, "n": n, "bad": row["seq"]})
            return False, n, row["seq"]
        prev = row["hash"]
        c.update({"seq": row["seq"], "hash": prev, "n": n})
    return True, n, None


def _warninglists_status() -> dict[str, Any]:
    try:
        from hydra.enrich import warninglists

        s = warninglists.summary()
        return {"line": warninglists.report_line(),
                **{k: s.get(k) for k in ("available", "loaded", "mapped", "commit", "by_effect")}}
    except Exception:  # noqa: BLE001 — status must never fail on an optional data snapshot
        return {"available": False}


@router.get("/status")
def app_status(request: Request) -> dict[str, Any]:
    """Everything the status bar needs in one poll: offline/enrichment mode, run, audit chain, jobs."""
    st = _st(request)
    enrich_on = os.environ.get("HYDRA_ENRICH", "0") == "1" and not st.settings.offline
    with st.reader() as conn:
        ok, n, bad = verify_chain_cached(conn, str(st.settings.db_path))
        run_id = resolve_run(conn)
        eval_id = resolve_run(conn, None, "eval")
        active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    return {
        "offline": st.settings.offline,
        "enrichment_mode": "live" if enrich_on else "replay",
        "warninglists": _warninglists_status(),
        "run_id": run_id,
        "eval_run_id": eval_id,
        "audit": {"ok": ok, "events": n, "first_bad_seq": bad},
        "active_jobs": active,
        "reviews": reviews,
        "reviews_change_grouping": _apply_review_fn() is not None,
        "contracts_version": CONTRACTS_VERSION,
        "version": __import__("hydra").__version__,
    }


# --------------------------------------------------------------------------------------- incidents


def _membership(conn: sqlite3.Connection, run_id: str | None) -> dict[str, str]:
    if not run_id:
        return {}
    return {r["incident_id"]: r["campaign_id"] for r in conn.execute(
        "SELECT incident_id, campaign_id FROM campaign_members WHERE run_id=?", (run_id,))}


def _incident_summary(conn: sqlite3.Connection, row: sqlite3.Row, campaign: str | None = None) -> dict[str, Any]:
    inc_id = row["id"]
    roles = Counter(r["role"] for r in conn.execute(
        "SELECT role FROM incident_artifacts WHERE incident_id=?", (inc_id,)))
    fams = {r["family"]: r["n"] for r in conn.execute(
        "SELECT family, COUNT(*) AS n FROM genes WHERE incident_id=? AND status='observed' GROUP BY family",
        (inc_id,))}
    thumb = conn.execute(
        "SELECT ia.artifact_id FROM incident_artifacts ia JOIN artifacts a ON a.id=ia.artifact_id"
        " WHERE ia.incident_id=? AND a.mime LIKE 'image/%' AND a.mime != 'image/svg+xml' ORDER BY CASE ia.role"
        " WHEN 'ad_creative' THEN 0 WHEN 'feed_screenshot' THEN 1 WHEN 'landing_screenshot' THEN 2 ELSE 3 END,"
        " ia.path_in_incident LIMIT 1", (inc_id,)).fetchone()
    return {
        "id": inc_id,
        "source": display_source(row["source"], row["source_ref"]),
        "source_ref": safe_ref(row["source_ref"]),
        "title": row["title"],
        "observed_at": row["observed_at"],
        "meta": _json(row["meta"], {}),
        "artifact_roles": dict(roles),
        "artifact_count": sum(roles.values()),
        "observed_genes": fams,
        "thumbnail": thumb["artifact_id"] if thumb else None,
        "campaign_id": campaign,
    }


INCIDENT_COLS = "id, source, source_ref, title, observed_at, meta, created_at"  # never `label`


@router.get("/incidents")
def list_incidents(
    request: Request,
    run_id: str | None = None,
    q: str | None = None,
    campaign_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        members = _membership(conn, run)
        where, args = [], []
        if q:
            where.append("(id LIKE ? OR title LIKE ? OR source_ref LIKE ?)")
            args += [f"%{q}%"] * 3
        if campaign_id:
            where.append("id IN (SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=?)")
            args += [run, campaign_id]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM incidents {clause}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT {INCIDENT_COLS} FROM incidents {clause} ORDER BY id LIMIT ? OFFSET ?", [*args, limit, offset]
        ).fetchall()
        items = [_incident_summary(conn, r, members.get(r["id"])) for r in rows]
        assigned = {r["incident_id"]: r["decision"]
                    for r in _latest_assignments(conn, run, incident_ids=[r["id"] for r in rows])}
        for it in items:
            it["assignment"] = assigned.get(it["id"])
    return _page(items, total, limit, offset, run)


def _genes_by_family(conn: sqlite3.Connection, incident_id: str, per_family: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, family, kind, value, vector_ref, quality, status, provenance, evidence, extractor,"
        " dependency_group FROM genes WHERE incident_id=? ORDER BY family, kind, id", (incident_id,)
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        g = dict(r)
        g["evidence"] = _json(g["evidence"], {})
        grouped[g["family"]].append(g)
    out = []
    for fam in FAMILY_ORDER + sorted(set(grouped) - set(FAMILY_ORDER)):
        genes = grouped.get(fam, [])
        by_status = Counter(g["status"] for g in genes)
        if not genes:
            fam_status = "unknown"  # nothing extracted: unknown, not evidence
        elif by_status.get("observed"):
            fam_status = "observed"
        else:
            fam_status = by_status.most_common(1)[0][0]
        out.append({
            "family": fam,
            "status": fam_status,
            "counts": dict(by_status),
            "kinds": dict(Counter(g["kind"] for g in genes)),
            "genes": genes[:per_family],
            "truncated": max(0, len(genes) - per_family),
        })
    return out


def _enrichment_for(conn: sqlite3.Connection, keys: set[str], queries: set[str]) -> list[dict[str, Any]]:
    if not keys and not queries:
        return []
    clauses, args = [], []
    if keys:
        clauses.append(f"key IN ({','.join('?' * len(keys))})")
        args += sorted(keys)
    if queries:
        clauses.append(f"query IN ({','.join('?' * len(queries))})")
        args += sorted(queries)
    rows = conn.execute(
        f"SELECT * FROM enrichment WHERE {' OR '.join(clauses)} ORDER BY source, query, fetched_at DESC", args
    ).fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        rec["summary"] = _json(rec["summary"], {})
        out.append(rec)
    return out


def _artifacts_of(conn: sqlite3.Connection, incident_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT ia.artifact_id, ia.role, ia.path_in_incident, ia.meta, a.mime, a.size, a.status"
        " FROM incident_artifacts ia LEFT JOIN artifacts a ON a.id=ia.artifact_id"
        " WHERE ia.incident_id=? ORDER BY ia.role, ia.path_in_incident", (incident_id,)
    ).fetchall()
    out = []
    for r in rows:
        art = dict(r)
        art["meta"] = _json(art["meta"], {})
        out.append(art)
    return out


@router.get("/incidents/{incident_id}")
def get_incident(
    request: Request, incident_id: str, run_id: str | None = None, per_family: int = Query(300, ge=1, le=5000)
) -> dict[str, Any]:
    with _st(request).reader() as conn:
        row = conn.execute(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="incident not found")
        run = resolve_run(conn, run_id)
        members = _membership(conn, run)
        inc = _incident_summary(conn, row, members.get(incident_id))
        inc["artifacts"] = _artifacts_of(conn, incident_id)
        inc["families"] = _genes_by_family(conn, incident_id, per_family)
        keys = {r[0] for r in conn.execute(
            "SELECT DISTINCT json_extract(evidence, '$.enrichment_key') FROM genes WHERE incident_id=?"
            " AND json_extract(evidence, '$.enrichment_key') IS NOT NULL", (incident_id,))}
        queries = {r[0] for r in conn.execute(
            "SELECT DISTINCT value FROM genes WHERE incident_id=? AND family='INF' AND value IS NOT NULL"
            " AND kind IN ('domain','host','ip','landing_host','registrable_domain')", (incident_id,))}
        inc["enrichment"] = _enrichment_for(conn, keys, queries)
        # Genes that cite an enrichment key without a stored record: summarise them from the genes, honestly
        # marked as lacking source metadata (ADR-007 wants source + fetched_at on every record).
        have = {e["key"] for e in inc["enrichment"]}
        derived: dict[str, dict[str, Any]] = {}
        for r in conn.execute(
            "SELECT kind, value, evidence, extractor FROM genes WHERE incident_id=? AND status='observed'"
            " AND json_extract(evidence, '$.enrichment_key') IS NOT NULL ORDER BY id", (incident_id,)
        ):
            g = {"kind": r["kind"], "value": r["value"], "extractor": r["extractor"],
                 "evidence": _json(r["evidence"], {}) or {}}
            k = g["evidence"].get("enrichment_key")
            if not k or k in have:
                continue
            note = str(g["evidence"].get("note") or "")
            rec = derived.setdefault(k, {"key": k, "source": g["extractor"], "query": note.split(":")[0] or None,
                                         "fetched_at": None, "status": "observed", "response_sha256": None,
                                         "cache_path": None, "summary": {}, "derived_from_genes": True})
            rec["summary"].setdefault(g["kind"], g["value"])
        inc["enrichment"] += sorted(derived.values(), key=lambda r: r["query"] or "")
        inc["run_id"] = run
        inc["assignment"] = None
        inc["neighbours"] = []
        if run:
            latest = _latest_assignments(conn, run, incident_ids=[incident_id])
            inc["assignment"] = _json(latest[0]["detail"]) if latest else None
            runs = _assignment_runs(conn, run)
            rmarks = ",".join("?" * len(runs))
            # one row per neighbour: the newest run wins (the same pair can be scored in several assign runs)
            order = {rid: i for i, rid in enumerate(runs)}
            best: dict[str, dict[str, Any]] = {}
            for r in conn.execute(
                f"SELECT a, b, run_id, probability, total_bits, hypothesis FROM pairs WHERE run_id IN ({rmarks})"
                " AND (a=? OR b=?)", (*runs, incident_id, incident_id)
            ):
                other = r["b"] if r["a"] == incident_id else r["a"]
                if other not in best or order[r["run_id"]] > best[other]["_o"]:
                    best[other] = {"other": other, "probability": r["probability"], "total_bits": r["total_bits"],
                                   "hypothesis": r["hypothesis"], "_o": order[r["run_id"]]}
            inc["neighbours"] = sorted(({k: v for k, v in n.items() if k != "_o"} for n in best.values()),
                                       key=lambda n: (-n["probability"], -n["total_bits"]))[:20]
    return inc


# --------------------------------------------------------------------------------------- campaigns


def _campaign_row(row: sqlite3.Row) -> dict[str, Any]:
    detail = _json(row["detail"], {}) or {}
    detail.setdefault("id", row["id"])
    detail["label"] = detail.get("label") or row["label"]
    detail["confidence"] = row["confidence"]
    detail["status"] = row["status"]
    return detail


@router.get("/campaigns")
def list_campaigns(
    request: Request,
    run_id: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return _page([], 0, limit, offset, None)
        total = conn.execute("SELECT COUNT(*) FROM campaigns WHERE run_id=?", (run,)).fetchone()[0]
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM campaign_members m WHERE m.run_id=c.run_id AND m.campaign_id=c.id)"
            " AS n FROM campaigns c WHERE c.run_id=? ORDER BY n DESC, c.confidence DESC, c.id LIMIT ? OFFSET ?",
            (run, limit, offset),
        ).fetchall()
        items = []
        group_of = {cid: g for g in _groups(conn, run) for cid in (g.get("campaign_ids") or [])}
        for r in rows:
            c = _campaign_row(r)
            c["size"] = r["n"]
            g = group_of.get(c["id"])
            if g:
                c["group_id"] = g["id"]
                c["group_label"] = g.get("label")
            c["core_genome"] = (c.get("core_genome") or [])[:5]
            items.append(c)
        from hydra import dataflow

        if dataflow.ensure(_st(request).settings.db_path, _st(request).writer, conn, run):  # DATAFLOW: live sizes
            dataflow.campaigns_page_view(conn, run, items)
        page = _page(items, total, limit, offset, run)
        page["live"] = dataflow.live_info(conn, run)
    return page


def _dominant_family(family_bits: dict[str, float]) -> str | None:
    positive = {k: v for k, v in (family_bits or {}).items() if v and v > 0}
    return max(positive, key=positive.get) if positive else None  # type: ignore[arg-type]


def _campaign_card(conn: sqlite3.Connection, run: str, campaign_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM campaigns WHERE run_id=? AND id=?", (run, campaign_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found in this run")
    card = _campaign_row(row)
    member_ids = [r["incident_id"] for r in conn.execute(
        "SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=? ORDER BY incident_id",
        (run, campaign_id))]
    if not member_ids:
        member_ids = list(card.get("members") or [])
    marks = ",".join("?" * len(member_ids)) or "NULL"
    inc_rows = conn.execute(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id IN ({marks})", member_ids).fetchall()
    card["members"] = member_ids
    card["member_incidents"] = [_incident_summary(conn, r, campaign_id) for r in inc_rows]
    # audit B15: material assigned to this campaign after the analysis counts in the live size; it is listed
    # separately (not a clustered member: pairs, consistency and lineage below stay on the clustered members)
    from hydra.dataflow import assigned_members

    known = set(member_ids)
    assigned = [i for i in assigned_members(conn, run, [campaign_id]).get(campaign_id, []) if i not in known]
    if assigned:
        q = ",".join("?" * len(assigned))
        for r in conn.execute(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id IN ({q}) ORDER BY id", assigned):
            card["member_incidents"].append({**_incident_summary(conn, r, campaign_id), "membership": "assigned"})
    card["assigned_members"] = assigned
    card["base_size"] = len(member_ids)
    card["size"] = len(member_ids) + len(assigned)

    # Infrastructure + persons: observed member genes, value -> incidents carrying it.
    def collect(family: str, kinds: tuple[str, ...] | None) -> dict[str, list[dict[str, Any]]]:
        kind_clause = f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""
        rows = conn.execute(
            f"SELECT kind, value, provenance, incident_id FROM genes WHERE incident_id IN ({marks})"
            f" AND family=? AND status='observed' AND value IS NOT NULL{kind_clause}",
            [*member_ids, family, *(kinds or ())],
        ).fetchall()
        acc: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            key = (r["kind"], r["value"])
            ent = acc.setdefault(key, {"kind": r["kind"], "value": r["value"], "provenance": r["provenance"],
                                       "incidents": set()})
            ent["incidents"].add(r["incident_id"])
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ent in acc.values():
            ent["incidents"] = sorted(ent["incidents"])
            ent["share"] = len(ent["incidents"]) / max(1, len(member_ids))
            out[ent["kind"]].append(ent)
        for kind in out:
            out[kind].sort(key=lambda e: (-len(e["incidents"]), e["value"]))
        return dict(out)

    card["infrastructure"] = collect("INF", None)
    card["host_count"] = len({e["value"] for k in HOST_KINDS for e in card["infrastructure"].get(k, [])})
    card["persons"] = collect("ID", None)
    card["timeline"] = sorted(
        ({"incident_id": r["id"], "observed_at": r["observed_at"], "title": r["title"]} for r in inc_rows),
        key=lambda e: (e["observed_at"] is None, e["observed_at"] or ""),
    )
    member_set = set(member_ids)
    internal, external = [], []
    for r in conn.execute(
        f"SELECT a, b, probability, total_bits, hypothesis, detail FROM pairs WHERE run_id=?"
        f" AND (a IN ({marks}) OR b IN ({marks})) ORDER BY probability DESC",
        [run, *member_ids, *member_ids],
    ):
        detail = _json(r["detail"], {}) or {}
        edge = {"a": r["a"], "b": r["b"], "probability": r["probability"], "total_bits": r["total_bits"],
                "hypothesis": r["hypothesis"], "dominant_family": _dominant_family(detail.get("family_bits", {}))}
        if r["a"] in member_set and r["b"] in member_set:
            internal.append(edge)
        else:
            external.append(edge)
    others = _membership(conn, run)
    for e in external:
        outside = e["b"] if e["a"] in member_set else e["a"]
        e["other_campaign"] = others.get(outside)
    group = next((g for g in _groups(conn, run) if card["id"] in (g.get("campaign_ids") or [])), None)
    card["group"] = group
    if group:
        card["group_id"] = group["id"]
    card["internal_pairs"] = internal
    card["cross_links"] = external[:50]
    card["run_id"] = run
    return card


@router.get("/campaigns/{campaign_id}")
def get_campaign(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        return _campaign_card(conn, run, campaign_id)


@router.get("/campaigns/{campaign_id}/export")
def export_campaign(
    request: Request, campaign_id: str, format: Literal["json", "csv"] = "json", run_id: str | None = None
) -> Response:
    """Campaign card as JSON, or one CSV row per member gene (pivot keys for a report)."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        card = _campaign_card(conn, run, campaign_id)
        safe = safe_filename(campaign_id, "campaign")
        if format == "json":
            body = json.dumps(redact_obj(json.loads(json.dumps(card, default=str))), ensure_ascii=False, indent=2)
            record_export(_st(request).writer, "campaign_json", run, {"members": len(card["members"])},
                          campaign_id=campaign_id)
            return Response(body, media_type="application/json", headers=content_disposition(f"campaign-{safe}.json"))
        marks = ",".join("?" * len(card["members"])) or "NULL"
        rows = conn.execute(
            f"SELECT incident_id, family, kind, value, status, provenance, quality, extractor FROM genes"
            f" WHERE incident_id IN ({marks}) AND vector_ref IS NULL ORDER BY incident_id, family, kind",
            card["members"],
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["run_id", "campaign_id", "incident_id", "family", "kind", "value", "status", "provenance",
                "quality", "extractor"])
    for r in rows:
        w.writerow(csv_row([run, campaign_id, *[r[k] for k in r.keys()]]))
    record_export(_st(request).writer, "campaign_csv", run, {"members": len(card["members"]), "rows": len(rows)},
                  campaign_id=campaign_id)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers=content_disposition(f"campaign-{safe}.csv"))


# --------------------------------------------------------------------------------------- reviews (state)


def _undone_ids(conn: sqlite3.Connection) -> set[str]:
    return {r["undoes"] for r in conn.execute("SELECT undoes FROM reviews WHERE action='undo' AND undoes IS NOT NULL")}


def _review_dict(row: sqlite3.Row, undone: set[str]) -> dict[str, Any]:
    rv = dict(row)
    rv["target"] = _json(rv["target"], {})
    rv["undone"] = rv["id"] in undone
    return rv


def _pair_reviews(conn: sqlite3.Connection, unsure: bool = True) -> dict[tuple[str, str], str]:
    """Effective analyst decision per pair: last accept/reject that was not undone; with `unsure`, an active
    "niepewne" mark (audit log, `hydra.review.unsure_marks`) shows as "unsure" when there is no decision."""
    undone = _undone_ids(conn)
    state: dict[tuple[str, str], str] = {}
    if unsure:
        from hydra.review import unsure_marks

        for m in unsure_marks(conn):
            t = m.get("target") or {}
            if t.get("a") and t.get("b"):
                state[tuple(sorted((t["a"], t["b"])))] = "unsure"  # type: ignore[index]
    for r in conn.execute(
        "SELECT id, action, target FROM reviews WHERE action IN ('accept_link','reject_link') ORDER BY created_at, id"
    ):
        if r["id"] in undone:
            continue
        t = _json(r["target"], {}) or {}
        a, b = t.get("a"), t.get("b")
        if a and b:
            state[tuple(sorted((a, b)))] = "accepted" if r["action"] == "accept_link" else "rejected"  # type: ignore[index]
    return state


# --------------------------------------------------------------------------------------- graph + pairs


def _task_graph_payload(conn: sqlite3.Connection, run: str, reviews: dict[tuple[str, str], str], min_p: float,
                        family: str | None, campaign_id: str | None, max_edges: int, extra_sql: str,
                        extra_args: list[Any]) -> dict[str, Any]:
    members = _membership(conn, run)
    labels = {r["id"]: r["label"] for r in conn.execute("SELECT id, label FROM campaigns WHERE run_id=?", (run,))}
    edges_total = conn.execute("SELECT COUNT(*) FROM pairs WHERE run_id=?", (run,)).fetchone()[0]
    edges = []
    for r in conn.execute(
        "SELECT a, b, probability, total_bits, hypothesis, calibrated, detail FROM pairs"
        f" WHERE run_id=? AND probability>=?{extra_sql} ORDER BY probability DESC", (run, min_p, *extra_args)
    ):
        detail = _fast_json(r["detail"])
        fam_bits = detail.get("family_bits", {}) or {}
        dom = _dominant_family(fam_bits)
        if family and dom != family:
            continue
        if campaign_id and members.get(r["a"]) != campaign_id and members.get(r["b"]) != campaign_id:
            continue
        edges.append({
            "id": f"{r['a']}|{r['b']}", "source": r["a"], "target": r["b"],
            "kind": dom or "none", "weight": r["probability"], "probability": r["probability"],
            "total_bits": r["total_bits"], "hypothesis": r["hypothesis"], "calibrated": bool(r["calibrated"]),
            "dominant_family": dom, "family_bits": fam_bits, "review": reviews.get((r["a"], r["b"])),
        })
        if len(edges) >= max_edges:
            break
    node_ids: set[str] = {e["source"] for e in edges} | {e["target"] for e in edges}
    if campaign_id:
        node_ids |= {i for i, c in members.items() if c == campaign_id}
    else:
        node_ids |= set(members)
        node_ids |= set(run_incidents(conn, run))
    marks = ",".join("?" * len(node_ids)) or "NULL"
    inc = {r["id"]: r for r in conn.execute(
        f"SELECT id, title, observed_at FROM incidents WHERE id IN ({marks})", sorted(node_ids))}
    degree = Counter([e["source"] for e in edges] + [e["target"] for e in edges])
    nodes = [{
        "id": n, "label": n, "title": inc[n]["title"] if n in inc else None,
        "group": members.get(n), "campaign_label": labels.get(members.get(n, ""), None),
        "observed_at": inc[n]["observed_at"] if n in inc else None, "size": 4 + min(10, degree[n]),
        "degree": degree[n],
    } for n in sorted(node_ids)]
    return {"run_id": run, "nodes": nodes, "edges": edges,
            "stats": {"nodes": len(nodes), "edges": len(edges), "edges_total": edges_total}}


@router.get("/graph")
def graph(
    request: Request,
    run_id: str | None = None,
    min_p: float = Query(0.0, ge=0.0, le=1.0),
    family: str | None = Query(None, description="keep edges whose dominant family is this"),
    campaign_id: str | None = None,
    max_edges: int = Query(20000, ge=1, le=200000),
    min_bits: float | None = Query(None, description="keep pairs with total_bits >= this (filtered in SQL)"),
    links_only: bool = Query(False, description="drop pairs scored independent (filtered in SQL)"),
) -> dict[str, Any]:
    extra_sql, extra_args = "", []
    if min_bits is not None:
        extra_sql += " AND total_bits>=?"
        extra_args.append(min_bits)
    if links_only:
        extra_sql += " AND hypothesis!='independent'"
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "edges_total": 0}}
        reviews = _pair_reviews(conn)
        build = lambda: _task_graph_payload(conn, run, reviews, min_p, family, campaign_id, max_edges,  # noqa: E731
                                            extra_sql, extra_args)
        fp = api_cache.run_fingerprint(conn, run) if api_cache.enabled() else None
        if fp is None:
            return build()
        key = ["task-graph", run, fp, api_cache.reviews_digest(reviews),
               [min_p, family, campaign_id, max_edges, min_bits, links_only]]
        return api_cache.memo_response(request, key, build)


@router.get("/pairs")
def list_pairs(
    request: Request,
    run_id: str | None = None,
    min_p: float = Query(0.0, ge=0.0, le=1.0),
    max_p: float = Query(1.0, ge=0.0, le=1.0),
    hypothesis: str | None = None,
    incident: str | None = None,
    order: Literal["probability", "uncertainty"] = "probability",
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return _page([], 0, limit, offset, None)
        where = ["run_id=?", "probability>=?", "probability<=?"]
        args: list[Any] = [run, min_p, max_p]
        if hypothesis:
            where.append("hypothesis=?")
            args.append(hypothesis)
        if incident:
            where.append("(a=? OR b=?)")
            args += [incident, incident]
        clause = " AND ".join(where)
        sort = "ABS(probability-0.5) ASC" if order == "uncertainty" else "probability DESC"
        total = conn.execute(f"SELECT COUNT(*) FROM pairs WHERE {clause}", args).fetchone()[0]
        reviews = _pair_reviews(conn)
        items = []
        for r in conn.execute(
            f"SELECT a, b, probability, total_bits, hypothesis, calibrated, model, detail FROM pairs WHERE {clause}"
            f" ORDER BY {sort}, total_bits DESC, a, b LIMIT ? OFFSET ?", [*args, limit, offset]
        ):
            detail = _json(r["detail"], {}) or {}
            items.append({
                "a": r["a"], "b": r["b"], "probability": r["probability"], "total_bits": r["total_bits"],
                "hypothesis": r["hypothesis"], "calibrated": bool(r["calibrated"]), "model": r["model"],
                "family_bits": detail.get("family_bits", {}),
                "missing_families": detail.get("missing_families", []),
                "dominant_family": _dominant_family(detail.get("family_bits", {})),
                "review": reviews.get((r["a"], r["b"])),
            })
    return _page(items, total, limit, offset, run)


HOST_KINDS = ("landing_host", "registrable_domain", "domain", "host", "linked_host", "domain_contacted",
              "host_contacted")
AD_ROLES = ("ad_creative", "feed_screenshot", "landing_screenshot", "ad_video", "screen_recording")


def _is_visual(mime: str | None) -> bool:
    return bool(mime) and (mime.startswith("image/") or mime.startswith("video/")) and mime != "image/svg+xml"


def _pair_side(conn: sqlite3.Connection, incident_id: str, campaign: str | None) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        return None
    side = _incident_summary(conn, row, campaign)
    arts = [a for a in _artifacts_of(conn, incident_id) if _is_visual(a["mime"])]
    by_id = {a["artifact_id"]: a for a in arts}
    # 1) the ad itself (creatives, screenshots, recordings), 2) images the VIS extractors used, 3) page images.
    visuals = sorted((a for a in arts if a["role"] in AD_ROLES), key=lambda a: AD_ROLES.index(a["role"]))
    seen = {a["artifact_id"] for a in visuals}
    for r in conn.execute(
        "SELECT DISTINCT json_extract(evidence, '$.artifact_id') AS aid FROM genes"
        " WHERE incident_id=? AND family='VIS' AND status='observed'", (incident_id,)
    ):
        if r["aid"] in by_id and r["aid"] not in seen:
            visuals.append(by_id[r["aid"]])
            seen.add(r["aid"])
    visuals += [a for a in arts if a["artifact_id"] not in seen and a["role"] == "landing_asset"]
    side["visuals"] = visuals[:8]
    title = conn.execute(
        "SELECT value FROM genes WHERE incident_id=? AND kind='page_title' AND status='observed' AND value IS NOT NULL"
        " LIMIT 1", (incident_id,)).fetchone()
    landing = [a for a in _artifacts_of(conn, incident_id) if a["role"] == "landing_html"]
    side["landing_title"] = (title["value"] if title else None) or next(
        (a["meta"].get("title") for a in landing if a["meta"].get("title")), None)
    marks = ",".join("?" * len(HOST_KINDS))
    side["hosts"] = [r["value"] for r in conn.execute(
        f"SELECT value, MIN(CASE kind {' '.join(f'WHEN ? THEN {i}' for i in range(len(HOST_KINDS)))} END) AS o"
        f" FROM genes WHERE incident_id=? AND family='INF' AND kind IN ({marks}) AND status='observed'"
        " AND value IS NOT NULL GROUP BY value ORDER BY o, value LIMIT 12",
        (*HOST_KINDS, incident_id, *HOST_KINDS))]
    return side


@router.get("/pairs/{a}/{b}")
def get_pair(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    """PairScore (reasons waterfall) plus both incidents side by side and the analyst decision history."""
    lo, hi = sorted((a, b))
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        row = None
        for rid in reversed(_assignment_runs(conn, run)):  # newest assign run first, the analyse run last
            row = conn.execute("SELECT * FROM pairs WHERE run_id=? AND a=? AND b=?", (rid, lo, hi)).fetchone()
            if row is not None:
                break
        if row is None:
            raise HTTPException(status_code=404, detail="pair not scored in this run (not a candidate pair)")
        score = _json(row["detail"], {}) or {}
        score.update({"a": lo, "b": hi, "probability": row["probability"], "total_bits": row["total_bits"],
                      "hypothesis": row["hypothesis"], "calibrated": bool(row["calibrated"]), "model": row["model"]})
        score.setdefault("family_bits", {})
        score.setdefault("reasons", [])
        score.setdefault("contradictions", [])
        score.setdefault("missing_families", [])
        # Families with no bits and not listed as missing are still unknown for the UI.
        score["unknown_families"] = [f for f in FAMILY_ORDER if f not in score["family_bits"]]
        members = _membership(conn, run)
        undone = _undone_ids(conn)
        history = []
        for r in conn.execute("SELECT * FROM reviews ORDER BY created_at, id"):
            rv = _review_dict(r, undone)
            t = rv["target"] or {}
            if sorted((t.get("a") or "", t.get("b") or "")) == [lo, hi]:
                history.append(rv)
        return {
            "run_id": row["run_id"],
            "score": score,
            "dominant_family": _dominant_family(score["family_bits"]),
            "incident_a": _pair_side(conn, lo, members.get(lo)),
            "incident_b": _pair_side(conn, hi, members.get(hi)),
            "same_campaign_in_run": members.get(lo) is not None and members.get(lo) == members.get(hi),
            "review": _pair_reviews(conn).get((lo, hi)),
            "reviews": history,
        }


# --------------------------------------------------------------------------------------- assignments


@router.get("/assignments")
def list_assignments(
    request: Request,
    run_id: str | None = None,
    decision: str | None = None,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Newest assignment per incident against the analyse run (its own rows + `assign` runs on top of it)."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return _page([], 0, limit, offset, None)
        rows = _latest_assignments(conn, run, decision=decision)
        items = []
        for r in rows[offset:offset + limit]:
            detail = _json(r["detail"], {}) or {}
            detail.update({"incident_id": r["incident_id"], "decision": r["decision"],
                           "campaign_id": r["campaign_id"], "created_at": r["created_at"],
                           "assign_run_id": r["run_id"]})
            items.append(detail)
    return _page(items, len(rows), limit, offset, run)


# --------------------------------------------------------------------------------------- analyse new material


class AnalyseJson(BaseModel):
    url: str | None = None
    path: str | None = None
    hint: str | None = Field(default=None, max_length=500)  # product name / keyword → corpus search


JUNK_REASON = "plik systemowy lub ukryty bez danych (np. .DS_Store, __MACOSX, .env)"


def _is_data_file(name: str, head: bytes) -> bool:
    """ADAPT's shared data-vs-junk rule (hydra.adapters.nask_folder.is_data_file, audit A03); a minimal local
    fallback keeps older builds working (OS junk dropped, dot-files with data extensions kept)."""
    try:
        from hydra.adapters.nask_folder import is_data_file  # type: ignore[attr-defined]
    except ImportError:
        parts = tuple(p for p in name.replace("\\", "/").split("/") if p not in ("", "."))
        if not parts or parts[-1].lower() in {".ds_store", "thumbs.db", "desktop.ini"} or parts[-1].startswith("._") \
                or any(p in {"__MACOSX", ".git"} for p in parts[:-1]):
            return False
        if not any(p.startswith(".") for p in parts):
            return True
        return parts[-1].lower().endswith((".har", ".json", ".html", ".htm", ".mhtml", ".mht"))
    return bool(is_data_file(name, head))


def _safe_rel(name: str, fallback: str) -> PurePosixPath:
    """Browser-supplied relative path (folder drops) without absolute parts or `..`."""
    parts = tuple(p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".", "..", "/"))
    return PurePosixPath(*parts[-12:]) if parts else PurePosixPath(fallback)


class PipelineUnavailable(RuntimeError):
    pass


def _pipeline_entry() -> Any:
    """CORE entry point: `hydra.pipeline.analyse_input(kind, payload, settings, writer=...)` (feat/core c8bd1d7).

    Returns `run(input_kind, path, url, hint) -> dict` with at least `assignment` (Assignment JSON) or, for a
    hint, `search`. Missing pipeline → the job fails visibly with PipelineUnavailable.
    """
    try:
        from hydra import pipeline  # type: ignore[attr-defined]
    except ImportError as exc:
        raise PipelineUnavailable("hydra.pipeline is not available in this build") from exc
    fn = getattr(pipeline, "analyse_input", None)
    if fn is None:
        raise PipelineUnavailable("hydra.pipeline.analyse_input is not available in this build")
    return fn


_CORE_KIND = {"files": "file", "path": "file", "screenshot": "screenshot", "url": "url", "hint": "hint"}


def _redact_url(url: str | None) -> str | None:
    from hydra.redact import redact_url

    return redact_url(url)


def _redact_text(text: str | None) -> str | None:
    from hydra.redact import redact_text

    return redact_text(text)


def is_corpus_folder(path: Path) -> bool:
    """A dataset folder holds several incident folders (NASK sample: one subfolder per incident) and no
    loose files besides hidden ones / a README. Anything else is one incident (folder, file or ZIP)."""
    if not path.is_dir():
        return False
    subdirs = [p for p in path.iterdir() if p.is_dir() and not p.name.startswith(".")]
    files = [p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")
             and p.suffix.lower() not in (".md", ".txt", ".csv", ".json")]
    return len(subdirs) >= 2 and not files


def _is_dataset(settings: Any, path: Path) -> bool:
    """The adapter's own rule (`classify_input`): several incidents → corpus analysis, not single-item assignment.
    An unreadable archive is not a dataset (the single-item path reports it per file)."""
    try:
        from hydra.adapters.nask_folder import classify_input

        return classify_input(path, settings, dataset=True) == "dataset"  # ZIP of incident ZIPs too (JURY-READY)
    except Exception:  # noqa: BLE001 — a rejected ZIP (bomb, too large) is reported by the single-item path
        return False


def _corpus_entry() -> Any:
    """CORE corpus run: `pipeline.analyse_corpus` when present, else `pipeline.analyse([path], settings, writer=)`."""
    try:
        from hydra import pipeline  # type: ignore[attr-defined]
    except ImportError as exc:
        raise PipelineUnavailable("hydra.pipeline is not available in this build") from exc
    fn = getattr(pipeline, "analyse_corpus", None) or getattr(pipeline, "analyse", None)
    if fn is None:
        raise PipelineUnavailable("hydra.pipeline.analyse_corpus is not available in this build")
    import inspect

    accepted = set(inspect.signature(getattr(pipeline, "analyse", fn)).parameters)

    def run(path: Path, settings: Any, writer: Any, **kw: Any) -> Any:
        # job_id ties CORE's progress file to this API job; corpus/use_cache only when CORE supports them
        extra = {k: v for k, v in kw.items() if k in accepted and v is not None}
        from hydra.jury_report import jury_analyse  # JURY-READY: dataset rules + per-file import report

        return jury_analyse([path], settings, entry=fn, writer=writer, **extra)

    return run


def _run_result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    out: dict[str, Any] = {"kind": "corpus"}
    for attr in ("run_id", "incident_ids", "metrics"):
        if hasattr(result, attr):
            out[attr] = getattr(result, attr)
    if isinstance(out.get("metrics"), dict):  # keep the card small: counts, not every extractor stat
        m = out["metrics"]
        out["metrics"] = {k: m[k] for k in ("incidents", "genes", "pairs_scored", "campaigns",
                                            "multi_member_campaigns", "hypotheses", "timings_s") if k in m}
        if m.get("import_report"):  # per-file statuses of the dataset import (JURY-READY), capped in CORE
            out["import_report"] = m["import_report"]
        if (m.get("references") or {}).get("notes_pl"):
            out["notes"] = list(m["references"]["notes_pl"])
    return out


def _submit_analyse(
    st: Any, *, input_kind: str, path: Path | None = None, url: str | None = None, hint: str | None = None,
    cleanup: Path | None = None, params: dict, corpus_kwargs: dict[str, Any] | None = None,
) -> str:
    job_id = create_job(st.writer, "analyse", params)

    def handler(ctx: JobContext) -> None:
        if path is not None:  # IMPORT-PROGRESS: per-file import counters (hydra/import_progress.py)
            from hydra.import_progress import tracking

            with tracking(st.settings, job_id, incidents_total=None if input_kind == "corpus" else 1,
                          input_ref=_redact_text(str(path))[:300]):
                return _handle(ctx)
        return _handle(ctx)

    def _handle(ctx: JobContext) -> None:
        try:
            if input_kind == "corpus":
                result = _run_result_dict(_corpus_entry()(path, st.settings, st.writer, job_id=job_id,
                                                          **(corpus_kwargs or {})))
            else:
                fn = _pipeline_entry()
                payload: Any = hint if input_kind == "hint" else url if input_kind == "url" else path
                result = fn(_CORE_KIND[input_kind], payload, st.settings, writer=st.writer)
            if hasattr(result, "model_dump"):  # an Assignment returned directly
                result = {"assignment": result.model_dump(mode="json")}
            result = json.loads(json.dumps(result, default=str))
            assignment = result.get("assignment") if isinstance(result, dict) else None

            def _tx(conn: sqlite3.Connection) -> None:
                events.append(conn, ANALYSE_EVENT, {"job_id": job_id, "input_kind": input_kind,
                                                    "assignment": assignment, "result": result})

            st.writer.run(_tx)
            from hydra.api_cache import warm_async

            warm_async(st)  # API-PERF: precompute the cached views of the new run (best effort, background)
        finally:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)

    st.runner.submit(job_id, handler)
    return job_id


def _spool_uploads(st: Any, uploads: list[UploadFile], skipped: list[dict[str, str]] | None = None) -> Path:
    cap = st.settings.max_upload_mb * 1024 * 1024
    skipped = skipped if skipped is not None else []
    spool = Path(tempfile.mkdtemp(dir=st.settings.tmp_dir, prefix="analyse-"))
    root = spool / "incident"
    total = 0
    try:
        for i, f in enumerate(uploads):
            rel = _safe_rel(f.filename or "", f"upload-{i}")
            head = f.file.read(4096)
            f.file.seek(0)
            if not _is_data_file(str(rel), head):
                skipped.append({"path": safe_ref(f.filename or ""), "reason": JUNK_REASON})
                continue
            target = root / rel
            if target.exists():
                target = target.with_name(f"{i:05d}-{target.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as out:
                while chunk := f.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > cap:
                        raise HTTPException(status_code=413, detail=f"upload exceeds {st.settings.max_upload_mb} MB")
                    out.write(chunk)
    except BaseException:
        shutil.rmtree(spool, ignore_errors=True)
        raise
    return spool


@router.post("/analyse", status_code=status.HTTP_202_ACCEPTED)
async def analyse(request: Request) -> dict[str, Any]:
    """Analyse new material. Four input kinds (NASK brief 13:30):

    - multipart `files` (incident folder files or one ZIP), optional form field `kind=screenshot` for a
      screenshot that contains a link (CORE: OCR → URL → follow);
    - JSON {url}; JSON {hint} (product name / keyword → corpus search); JSON {path} (local folder, CLI parity).
    """
    st = _st(request)
    from hydra import api_contract as contract  # G5-CONTRACT: optional Idempotency-Key (docs/INTEGRATION_CONTRACT.md)

    idem = contract.idempotency_key(request)

    async def submit(req_sha: str, response: dict[str, Any], **kw: Any) -> Any:
        params = kw.pop("params")
        return await run_in_threadpool(
            contract.submit_idempotent, st, idem, req_sha, response,
            lambda extra: _submit_analyse(st, params={**params, **extra}, **kw), kw.get("cleanup"))

    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        uploads = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
        kind = "screenshot" if str(form.get("kind") or "") == "screenshot" else "files"
        if not uploads:
            raise HTTPException(status_code=400, detail="no files in field 'files'")
        skipped: list[dict[str, str]] = []
        try:
            spool = await run_in_threadpool(_spool_uploads, st, uploads, skipped)
        finally:
            await form.close()
        root = spool / "incident"
        entries = list(root.iterdir()) if root.exists() else []
        if not entries:
            shutil.rmtree(spool, ignore_errors=True)
            raise HTTPException(status_code=400, detail={"message": "brak plików z danymi: wszystkie pominięto",
                                                         "skipped": skipped})
        # A single uploaded file/ZIP or a single dropped incident folder is handed over as itself.
        path = entries[0] if len(entries) == 1 else root
        if kind == "files" and _is_dataset(st.settings, path):  # dataset ZIP / multi-incident folder (B8)
            kind = "corpus"
        req_sha = await run_in_threadpool(contract.request_hash_tree, root, kind) if idem else ""
        return await submit(req_sha, {"mode": kind, "skipped": skipped}, input_kind=kind, path=path, cleanup=spool,
                            params={"source": "upload", "input_kind": kind, "files": len(uploads), "skipped": skipped})
    if not ctype.split(";")[0].strip().lower() == "application/json":
        raise HTTPException(status_code=415, detail="send multipart/form-data files or application/json")
    try:
        raw_body = await request.json()
        body = AnalyseJson.model_validate(raw_body)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="send multipart 'files' or JSON {url} | {hint} | {path}") from exc
    req_sha = contract.request_hash_json(raw_body) if idem else ""
    given = [k for k in ("url", "hint", "path") if (getattr(body, k) or "").strip()]
    if len(given) != 1:
        raise HTTPException(status_code=400, detail="give exactly one of url, hint or path")
    if body.hint and body.hint.strip():
        hint = body.hint.strip()
        return await submit(req_sha, {"mode": "hint"}, input_kind="hint", hint=hint,
                            params={"source": "hint", "hint": _redact_text(hint)})
    if body.url:
        url = body.url.strip()
        if not url.lower().startswith(("http://", "https://")) and re.match(
                r"^(www\.)?[\w-]+(\.[\w-]+)+([/?#]\S*)?$", url):
            url = "https://" + url  # 'pko-bonus24.com/oferta' typed without a scheme
        if not url.lower().startswith(("http://", "https://")) or len(url) > 4096:
            raise HTTPException(status_code=400, detail="url must start with http:// or https://")
        return await submit(req_sha, {"mode": "url"}, input_kind="url", url=url,
                            params={"source": "url", "url": _redact_url(url)})
    local = allowed_local_path(st.settings, body.path or "")
    mode = "corpus" if is_corpus_folder(local) or _is_dataset(st.settings, local) else "path"
    return await submit(req_sha, {"mode": mode}, input_kind=mode, path=local,
                        params={"source": "path", "mode": mode, "path": safe_ref(str(local))})


def allowed_local_path(settings: Any, raw: str) -> Path:
    """Audit A11: one policy for every endpoint (hydra.paths.check_input_path)."""
    from hydra.paths import PathNotAllowed, check_input_path

    try:
        return check_input_path(raw, settings)
    except PathNotAllowed as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/analyse/{job_id}")
def analyse_result(request: Request, job_id: str) -> dict[str, Any]:
    """Job progress, then the Assignment once the job is done."""
    with _st(request).reader() as conn:
        job = get_job(conn, job_id)
        if job is None or job["kind"] != "analyse":
            raise HTTPException(status_code=404, detail="analyse job not found")
        params = job.get("params") or {}
        if params.get("url"):
            params["url"] = _redact_url(params["url"])
        if params.get("hint"):
            params["hint"] = _redact_text(params["hint"])
        if params.get("path"):
            params["path"] = safe_ref(params["path"])
        job["params"] = params
        job["error"] = safe_ref(job.get("error"))
        row = conn.execute(
            "SELECT payload FROM events WHERE type=? AND json_extract(payload, '$.job_id')=? ORDER BY seq DESC LIMIT 1",
            (ANALYSE_EVENT, job_id),
        ).fetchone()
        payload = (_json(row["payload"], {}) or {}) if row else {}
        assignment = payload.get("assignment")
        result = payload.get("result")
        campaign = None
        if assignment and assignment.get("campaign_id"):
            run = resolve_run(conn)
            if run:
                c = conn.execute("SELECT * FROM campaigns WHERE run_id=? AND id=?",
                                 (run, assignment["campaign_id"])).fetchone()
                campaign = _campaign_row(c) if c else None
        live = _live_progress(conn, job, assignment, result, _st(request).store)
    return {"job": job, "assignment": assignment, "campaign": campaign, "result": result, "live": live}


def _live_progress(conn: sqlite3.Connection, job: dict[str, Any], assignment: dict[str, Any] | None,
                   result: dict[str, Any] | None, store: Any | None = None) -> dict[str, Any]:
    """Live stages of an analyse job, derived from the rows CORE writes (it reports no per-stage progress).

    The incident is the one named in the result, else the newest incident whose genes or row were written
    after the job started; the assign run is the `assign` run created after the job started. Stages whose
    rows are not there yet are reported as not reached, never estimated."""
    started = job.get("started_at")
    out: dict[str, Any] = {"incident_id": None, "artifacts_by_role": {}, "genes_by_family": {}, "genes_total": 0,
                           "assign_run_id": None, "pairs_scored": None, "top_pairs": [], "derived": True}
    iid = (result or {}).get("incident_id") or (assignment or {}).get("incident_id")
    if not iid and started:
        row = conn.execute("SELECT incident_id FROM genes WHERE created_at >= ? ORDER BY id DESC LIMIT 1",
                           (started,)).fetchone() or conn.execute(
            "SELECT id FROM incidents WHERE created_at >= ? ORDER BY created_at DESC LIMIT 1", (started,)).fetchone()
        iid = row[0] if row else None
    if iid:
        inc = conn.execute("SELECT title FROM incidents WHERE id=?", (iid,)).fetchone()
        out["incident_id"] = iid
        out["title"] = inc[0] if inc else None
        out["thumbnail"] = _best_image(conn, iid, store)
        out["artifacts_by_role"] = {r[0]: r[1] for r in conn.execute(
            "SELECT role, COUNT(*) FROM incident_artifacts WHERE incident_id=? GROUP BY 1", (iid,))}
        fam: dict[str, dict[str, int]] = defaultdict(dict)
        for r in conn.execute("SELECT family, status, COUNT(*) FROM genes WHERE incident_id=? GROUP BY 1, 2", (iid,)):
            fam[r[0]][r[1]] = r[2]
        out["genes_by_family"] = dict(fam)
        out["genes_total"] = sum(sum(v.values()) for v in fam.values())
    run_id = (result or {}).get("run_id")
    if not run_id and started:
        row = conn.execute("SELECT id FROM runs WHERE kind='assign' AND created_at >= ? ORDER BY created_at DESC"
                           " LIMIT 1", (started,)).fetchone()
        run_id = row[0] if row else None
    if run_id and iid:
        out["assign_run_id"] = run_id
        out["pairs_scored"] = conn.execute("SELECT COUNT(*) FROM pairs WHERE run_id=?", (run_id,)).fetchone()[0]
        out["top_pairs"] = [
            {"a": r["a"], "b": r["b"], "other": r["b"] if r["a"] == iid else r["a"], "probability": r["probability"],
             "total_bits": r["total_bits"], "hypothesis": r["hypothesis"]}
            for r in conn.execute("SELECT a, b, probability, total_bits, hypothesis FROM pairs WHERE run_id=?"
                                  " ORDER BY total_bits DESC LIMIT 8", (run_id,))]
    # matched campaign members for the end card
    cid = (assignment or {}).get("campaign_id") or next(
        (c.get("campaign_id") for c in (assignment or {}).get("candidates") or []), None)
    if cid:
        base = resolve_run(conn)
        out["campaign_members"] = [{"id": m[0], "thumbnail": _best_image(conn, m[0], store)} for m in conn.execute(
            "SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=? ORDER BY 1 LIMIT 8",
            (base, cid))] if base else []
        out["campaign_id_for_card"] = cid
    return out


# --------------------------------------------------------------------------------------- reviews


class ReviewIn(BaseModel):
    action: ReviewAction
    target: dict[str, Any] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=2000)
    analyst: str = Field(default="operator", max_length=64)  # used only with HYDRA_AUTH=0 (see acting_analyst)
    undoes: str | None = None


_TARGET_KEYS: dict[str, tuple[str, ...]] = {
    "accept_link": ("a", "b"),
    "reject_link": ("a", "b"),
    "merge_campaigns": ("campaign_ids",),
    "split_campaign": ("campaign_id",),
    "move_incident": ("incident_id", "campaign_id"),
}


def _apply_review_fn() -> Any | None:
    """CORE's apply_review (constraints + new clustering revision + undo), when the build has it (audit A04)."""
    for mod in ("hydra.review", "hydra.reviews", "hydra.pipeline"):
        try:
            m = __import__(mod, fromlist=["apply_review"])
        except ImportError:
            continue
        fn = getattr(m, "apply_review", None)
        if fn is not None:
            return fn
    return None


@router.post("/reviews", status_code=status.HTTP_201_CREATED)
def create_review(request: Request, body: ReviewIn) -> dict[str, Any]:
    """Analyst decision → reviews row + REVIEW audit event in one transaction. UNDO points at a review id."""
    st = _st(request)
    action = ReviewAction(body.action).value
    target = dict(body.target)
    if action == "undo":
        if not body.undoes:
            raise HTTPException(status_code=400, detail="undo needs 'undoes' (review id)")
    else:
        missing = [k for k in _TARGET_KEYS[action] if not target.get(k)]
        if missing:
            raise HTTPException(status_code=400, detail=f"target needs {missing}")
        if action in ("accept_link", "reject_link"):
            if target["a"] == target["b"]:
                raise HTTPException(status_code=400, detail="a pair needs two different incidents")
            target["a"], target["b"] = sorted((str(target["a"]), str(target["b"])))
    review = ReviewModel(id=uuid.uuid4().hex[:16], action=action, target=target,
                         analyst=acting_analyst(request, body.analyst),
                         note=body.note, created_at=events.utcnow(), undoes=body.undoes)

    def _tx(conn: sqlite3.Connection) -> str:
        if review.undoes:
            prev = conn.execute("SELECT action FROM reviews WHERE id=?", (review.undoes,)).fetchone()
            if prev is None:
                raise HTTPException(status_code=404, detail="review to undo not found")
            if prev["action"] == "undo":
                raise HTTPException(status_code=400, detail="cannot undo an undo; repeat the original decision")
            if conn.execute("SELECT 1 FROM reviews WHERE undoes=?", (review.undoes,)).fetchone():
                raise HTTPException(status_code=409, detail="review already undone")
        conn.execute(
            "INSERT INTO reviews (id, action, target, analyst, note, undoes, created_at) VALUES (?,?,?,?,?,?,?)",
            (review.id, review.action, events.canonical_json(review.target), review.analyst, review.note,
             review.undoes, review.created_at),
        )
        return events.append(conn, "REVIEW", review.model_dump(mode="json"))

    event_hash = st.writer.run(_tx)
    out: dict[str, Any] = {"review": review.model_dump(mode="json"), "event_hash": event_hash}
    fn = _apply_review_fn()
    if fn is None:
        out["effect"] = "annotation"  # recorded and audited; grouping unchanged until CORE applies reviews
        out["effect_pl"] = "adnotacja (nie zmienia grupowania)"
    else:
        try:
            res = fn(review.id, st.settings, st.writer)  # the review row is already stored (same writer)
            out["effect"] = "applied"
            out["applied"] = json.loads(json.dumps(res, default=str)) if res is not None else None
            from hydra.api_cache import warm_async

            warm_async(st)  # API-PERF: the revision run becomes the default run; precompute its views
        except Exception as exc:  # noqa: BLE001 — the review stays recorded; say clearly it was not applied
            out["effect"] = "annotation"
            out["effect_pl"] = f"adnotacja: zastosowanie nie powiodło się ({type(exc).__name__})"
    return out


@router.get("/reviews")
def list_reviews(request: Request, limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)) -> dict:
    with _st(request).reader() as conn:
        undone = _undone_ids(conn)
        total = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        items = [_review_dict(r, undone) for r in conn.execute(
            "SELECT * FROM reviews ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset))]
    return _page(items, total, limit, offset)


# --------------------------------------------------------------------------------------- search (T1 pivot)


@router.get("/search")
def search(request: Request, q: str = Query(..., min_length=2, max_length=200),
           limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Gene values, incidents and campaigns matching `q`; gene hits carry the incidents that share them."""
    like = f"%{q}%"
    with _st(request).reader() as conn:
        run = resolve_run(conn)
        incidents = [dict(r) for r in conn.execute(
            "SELECT id, title FROM incidents WHERE id LIKE ? OR title LIKE ? ORDER BY id LIMIT ?", (like, like, limit))]
        campaigns = [dict(r) for r in conn.execute(
            "SELECT id, label FROM campaigns WHERE run_id=? AND (id LIKE ? OR label LIKE ?) ORDER BY id LIMIT ?",
            (run, like, like, limit))] if run else []
        genes = [{"family": r["family"], "kind": r["kind"], "value": r["value"],
                  "incidents": sorted(set((r["ids"] or "").split(","))), "doc_freq": r["n"]}
                 for r in conn.execute(
            "SELECT family, kind, value, COUNT(DISTINCT incident_id) AS n, GROUP_CONCAT(DISTINCT incident_id) AS ids"
            " FROM genes WHERE status='observed' AND vector_ref IS NULL AND value LIKE ?"
            " GROUP BY family, kind, value ORDER BY n DESC, kind LIMIT ?", (like, limit))]
    return {"q": q, "run_id": run, "incidents": incidents, "campaigns": campaigns, "genes": genes}


# --------------------------------------------------------------------------------------- watchlist + scheduler (T1)
# CORE's `hydra.scheduler` (feat/core a1f7aa1) owns `<state_dir>/scheduler/{watchlist,status}.json`; these
# endpoints delegate to it. Without that module (older build) a plain `<state_dir>/watchlist.json` is used and
# the status says `available: false`. Every watchlist change gets a WATCHLIST_CHANGED audit event.


class WatchItem(BaseModel):
    kind: Literal["keyword", "domain"]
    value: str = Field(min_length=2, max_length=300)


class WatchlistChange(BaseModel):
    add: list[WatchItem] = Field(default_factory=list)
    remove: list[WatchItem] = Field(default_factory=list)


def _scheduler() -> Any | None:
    try:
        from hydra import scheduler  # type: ignore[attr-defined]
    except ImportError:
        return None
    return scheduler if hasattr(scheduler, "load_watchlist") else None


def _fallback_path(st: Any) -> Path:
    return Path(st.settings.state_dir) / "watchlist.json"


def _load_wl(st: Any) -> dict[str, Any]:
    mod = _scheduler()
    if mod is not None:
        return mod.load_watchlist(st.settings)
    path = _fallback_path(st)
    data = _json(path.read_text(encoding="utf-8"), {}) if path.is_file() else {}
    return {"keywords": list(data.get("keywords", [])), "domains": list(data.get("domains", []))}


def _save_wl(st: Any, wl: dict[str, Any]) -> None:
    mod = _scheduler()
    if mod is not None:
        mod.save_watchlist(st.settings, wl)
        return
    path = _fallback_path(st)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _wl_items(wl: dict[str, Any]) -> list[dict[str, str]]:
    return ([{"kind": "keyword", "value": k} for k in wl.get("keywords", [])]
            + [{"kind": "domain", "value": d} for d in wl.get("domains", [])])


@router.get("/watchlist")
def get_watchlist(request: Request) -> dict[str, Any]:
    wl = _load_wl(_st(request))
    return {"items": _wl_items(wl), "watchlist": wl}


@router.post("/watchlist")
def change_watchlist(request: Request, body: WatchlistChange) -> dict[str, Any]:
    st = _st(request)
    wl = _load_wl(st)
    key = {"keyword": "keywords", "domain": "domains"}

    def norm(item: Any) -> str:  # one canonical form for add and remove (CR-ASTRA-PIPE-306)
        return item.value.strip().lower() if item.kind == "domain" else item.value.strip()

    for item in body.remove:
        wl[key[item.kind]] = [v for v in wl.get(key[item.kind], []) if v != norm(item)]
    for item in body.add:
        value = norm(item)
        if value not in wl.setdefault(key[item.kind], []):
            wl[key[item.kind]].append(value)
    wl["keywords"] = sorted(wl.get("keywords", []))
    wl["domains"] = sorted(wl.get("domains", []))
    _save_wl(st, wl)
    change = {"add": [i.model_dump() for i in body.add], "remove": [i.model_dump() for i in body.remove]}
    st.writer.run(lambda conn: events.append(conn, "WATCHLIST_CHANGED", change))
    return {"items": _wl_items(wl), "watchlist": wl}


@router.get("/scheduler/status")
def scheduler_status(request: Request) -> dict[str, Any]:
    """CORE scheduler status (jobs, last runs, Meta token configured, inbox); `available: false` without it."""
    st = _st(request)
    mod = _scheduler()
    if mod is None:
        return {"available": False, "jobs": {}, "watchlist": _load_wl(st)}
    data = json.loads(json.dumps(mod.scheduler_status(st.settings), default=str))
    return {"available": True, **data}


@router.post("/scheduler/run")
def scheduler_run_now(request: Request) -> dict[str, Any]:
    """"Uruchom teraz": every scheduler job once in the background with the API's single Writer."""
    st = _st(request)
    mod = _scheduler()
    if mod is None or not hasattr(mod, "run_now_background"):
        return {"available": False, "started": False}
    started = mod.run_now_background(st.settings, st.writer)
    return {"available": True, "started": started, "note": None if started else "harmonogram już pracuje"}


# --------------------------------------------------------------------------------------- Neo4j export


@router.get("/export/neo4j")
def export_neo4j(
    request: Request,
    format: Literal["cypher", "zip", "csv", "nodes_csv", "edges_csv"] = "cypher",
    run_id: str | None = None,
    min_p: float = Query(0.0, ge=0.0, le=1.0),
    min_df: int = Query(2, ge=1, le=1000),
) -> Response:
    """Graph of the run for Neo4j, the same graph as `hydra export-neo4j` (hydra/export_neo4j.py): Incident,
    Campaign, CampaignGroup, Gene nodes; MEMBER_OF, IN_GROUP, HAS_GENE, LINKED_TO relationships.
    `cypher`: self-contained script (UNWIND + MERGE, rows inline) = `graph.cypher` of the CLI folder;
    `zip` (alias `csv`): the CLI folder (CSV files, `import.cypher` for LOAD CSV, `graph.cypher`);
    `nodes_csv` / `edges_csv`: `neo4j-admin database import` files."""
    import zipfile

    from hydra.export_neo4j import admin_csv, build_graph, inline_cypher, write_folder

    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        graph = build_graph(conn, run, min_df=min_df, min_p=min_p)
    disp = content_disposition  # ASCII-safe download names, no CR/LF from a run id (CR-ASTRA-SEC-7)
    kind = "zip" if format == "csv" else format
    counts = graph.counts()
    record_export(st.writer, f"neo4j_{kind}", run, {"incidents": counts["incidents"], "campaigns":
                  counts["campaigns"], "genes": counts["genes"], "edges": counts["linked_to"]})
    if format in ("nodes_csv", "edges_csv"):
        return Response(admin_csv(graph, format.removesuffix("_csv")), media_type="text/csv; charset=utf-8",
                        headers=disp(f"neo4j-{format.removesuffix('_csv')}-{run}.csv"))
    if format in ("zip", "csv"):
        tmp = Path(tempfile.mkdtemp(dir=st.settings.tmp_dir, prefix="neo4j-"))
        try:
            write_folder(graph, tmp / "neo4j")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in sorted((tmp / "neo4j").iterdir()):
                    zf.write(f, f"neo4j-{safe_filename(run, 'run')}/{f.name}")  # no path parts from an id
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return Response(buf.getvalue(), media_type="application/zip", headers=disp(f"neo4j-{run}.zip"))
    return Response(inline_cypher(graph), media_type="text/plain; charset=utf-8", headers=disp(f"hydra-{run}.cypher"))


# --------------------------------------------------------------------------------------- campaign groups (2.1)


@router.get("/campaign-groups")
def campaign_groups(request: Request, run_id: str | None = None) -> dict[str, Any]:
    """Groups of similar campaigns (contracts 2.1): `campaign_groups` table (migration v3), else
    `runs.metrics.campaign_groups`, else derived from `Campaign.group_id`."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None, "items": []}
        return {"run_id": run, "items": _groups(conn, run)}


def _groups(conn: sqlite3.Connection, run: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM campaigns WHERE run_id=? ORDER BY id", (run,)):
        c = _campaign_row(r)
        gid = c.get("group_id")
        if gid:
            g = groups.setdefault(gid, {"id": gid, "label": gid, "campaign_ids": [], "shared_genes": [],
                                        "confidence": 0.0})
            g["campaign_ids"].append(c["id"])
    run_row = _run_row(conn, run) or {}
    for full in (run_row.get("metrics") or {}).get("campaign_groups", []) or []:
        if isinstance(full, dict) and full.get("id"):
            groups.setdefault(full["id"], {"id": full["id"], "campaign_ids": []}).update(full)
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_groups'").fetchone()
    if has_table:
        for r in conn.execute("SELECT * FROM campaign_groups WHERE run_id=? ORDER BY id", (run,)):
            full = _json(r["detail"], {}) or {}
            full.update({"id": r["id"], "label": full.get("label") or r["label"], "confidence": r["confidence"]})
            groups.setdefault(r["id"], {"id": r["id"], "campaign_ids": []}).update(full)
    return sorted(groups.values(), key=lambda g: (-len(g.get("campaign_ids") or []), g["id"]))


# --------------------------------------------------------------------------------------- dashboard + simulation
# Aggregates for "Centrum dowodzenia" and "Symulacja potoku". Every number is counted from the tables or copied
# from runs.metrics written by CORE/EVAL; nothing is estimated here.

GENE_STATUSES = ("observed", "unavailable", "failed", "not_applicable")


_ENTROPY: dict[str, float] = {}
ROLE_BONUS = {"ad_creative": 0.6, "feed_screenshot": 0.4, "landing_screenshot": 0.3}


def image_entropy(path: Path) -> float:
    """Shannon entropy (bits) of the grey-level histogram of a 128-px thumbnail; near-blank images score low."""
    import math

    from PIL import Image

    try:
        with Image.open(path) as im:
            im.draft("L", (128, 128))
            g = im.convert("L")
            g.thumbnail((128, 128))
            hist = g.histogram()
    except Exception:  # noqa: BLE001 — unreadable image: never preferred
        return 0.0
    total = sum(hist) or 1
    return -sum((h / total) * math.log2(h / total) for h in hist if h)


def _best_image(conn: sqlite3.Connection, incident_id: str, store: Any | None = None) -> str | None:
    """Preview image: among the incident's creative / screenshot / VIS-evidence / page images, the one with the
    highest grey-level entropy plus a small role preference (ad > feed > landing screenshot), so a near-blank
    screenshot loses to a meaningful one. Without the store, the role order alone decides."""
    rows = conn.execute(
        "SELECT ia.artifact_id, ia.role, a.size FROM incident_artifacts ia JOIN artifacts a ON a.id=ia.artifact_id"
        " WHERE ia.incident_id=? AND a.mime LIKE 'image/%' AND a.mime!='image/svg+xml'", (incident_id,)).fetchall()
    vis = {r[0] for r in conn.execute(
        "SELECT DISTINCT json_extract(evidence, '$.artifact_id') FROM genes WHERE incident_id=? AND family='VIS'"
        " AND status='observed'", (incident_id,))}
    cands = [r for r in rows if r["role"] in ROLE_BONUS or r["artifact_id"] in vis]
    if not cands:
        cands = sorted(rows, key=lambda r: -(r["size"] or 0))[:6]
    if not cands:
        return None
    order = {"ad_creative": 0, "feed_screenshot": 1, "landing_screenshot": 2}
    cands = sorted(cands, key=lambda r: (order.get(r["role"], 3), -(r["size"] or 0)))[:12]
    if store is None:
        return cands[0]["artifact_id"]

    def score(r: sqlite3.Row) -> float:
        aid = r["artifact_id"]
        if aid not in _ENTROPY:
            try:
                _ENTROPY[aid] = image_entropy(store.safe_path(aid))
            except (ValueError, OSError):
                _ENTROPY[aid] = 0.0
        return _ENTROPY[aid] + ROLE_BONUS.get(r["role"], 0.0)

    return max(cands, key=score)["artifact_id"]


def run_incidents(conn: sqlite3.Connection, run: str | None, *, live: bool = True) -> list[str]:
    """Audit A13: the incidents a run covers (its campaign members) plus new items assigned against it.
    Without a run: every incident (nothing to scope to). `live=False` (DATAFLOW): only the immutable run (members
    and its own assignments); new material is overlaid from hydra.dataflow."""
    if not run:
        return [r[0] for r in conn.execute("SELECT id FROM incidents ORDER BY id")]
    if not live:
        from hydra.dataflow import base_scope

        return base_scope(conn, run)
    ids = {r[0] for r in conn.execute("SELECT incident_id FROM campaign_members WHERE run_id=?", (run,))}
    ids |= {r["incident_id"] for r in _latest_assignments(conn, run)}
    if not ids:  # a run without campaigns: fall back to its pairs
        ids = {x for r in conn.execute("SELECT a, b FROM pairs WHERE run_id=?", (run,)) for x in r}
    return sorted(ids)


def _in(ids: list[str]) -> str:
    return ",".join("?" * len(ids)) or "NULL"


def _family_matrix(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, dict[str, int]]]:
    out: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for r in conn.execute(f"SELECT incident_id, family, status, COUNT(*) AS n FROM genes WHERE incident_id IN"
                          f" ({_in(ids)}) GROUP BY 1, 2, 3", ids):
        out[r["incident_id"]][r["family"]][r["status"]] = r["n"]
    return {k: dict(v) for k, v in out.items()}


def _first_value(conn: sqlite3.Connection, incident_id: str, kind: str) -> str | None:
    r = conn.execute("SELECT value FROM genes WHERE incident_id=? AND kind=? AND status='observed'"
                     " AND value IS NOT NULL ORDER BY quality DESC, id LIMIT 1", (incident_id, kind)).fetchone()
    return r[0] if r else None


def _first_values(conn: sqlite3.Connection, ids: list[str], kind: str) -> dict[str, str]:
    """`_first_value` for many incidents in one query per 900 ids (highest quality, then lowest gene id)."""
    best: dict[str, tuple[float, int, str]] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in conn.execute(f"SELECT incident_id, value, quality, id FROM genes WHERE kind=? AND status='observed'"
                              f" AND value IS NOT NULL AND incident_id IN ({_in(chunk)})", (kind, *chunk)):
            key = (-(r[2] or 0), r[3], r[1])
            if r[0] not in best or key < best[r[0]]:
                best[r[0]] = key
    return {k: v[2] for k, v in best.items()}


def _best_images(conn: sqlite3.Connection, ids: list[str], store: Any | None, cache_root: Path | None = None
                 ) -> dict[str, str | None]:
    """`_best_image` for many incidents: batched queries and entropies from the persistent per-sha256 map
    (hydra.api_cache.entropies, computed in a thread pool on a miss). Same choice rule as `_best_image`."""
    import orjson

    from hydra.api_cache import entropies

    rows: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    vis: dict[str, set[str]] = defaultdict(set)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in conn.execute(
            "SELECT ia.incident_id, ia.artifact_id, ia.role, a.size FROM incident_artifacts ia JOIN artifacts a ON"
            f" a.id=ia.artifact_id WHERE ia.incident_id IN ({_in(chunk)}) AND a.mime LIKE 'image/%' AND"
            " a.mime!='image/svg+xml'", chunk):
            rows[r[0]].append((r[1], r[2], r[3]))
        for r in conn.execute(f"SELECT incident_id, evidence FROM genes WHERE incident_id IN ({_in(chunk)}) AND"
                              " family='VIS' AND status='observed' AND evidence LIKE '%artifact_id%'", chunk):
            try:
                aid = orjson.loads(r[1]).get("artifact_id")
            except (orjson.JSONDecodeError, AttributeError):
                continue
            if aid:
                vis[r[0]].add(aid)
    order = {"ad_creative": 0, "feed_screenshot": 1, "landing_screenshot": 2}
    cands: dict[str, list[tuple[str, str, int]]] = {}
    for iid in ids:
        rs = rows.get(iid, [])
        c = [r for r in rs if r[1] in ROLE_BONUS or r[0] in vis.get(iid, ())]
        if not c:
            c = sorted(rs, key=lambda r: -(r[2] or 0))[:6]
        if c:
            cands[iid] = sorted(c, key=lambda r: (order.get(r[1], 3), -(r[2] or 0)))[:12]
    if store is None:
        return {iid: (cands[iid][0][0] if iid in cands else None) for iid in ids}
    ent = entropies(cache_root, store, sorted({r[0] for c in cands.values() for r in c}), image_entropy, _ENTROPY)
    out: dict[str, str | None] = {}
    for iid in ids:
        c = cands.get(iid)
        out[iid] = max(c, key=lambda r: ent.get(r[0], 0.0) + ROLE_BONUS.get(r[1], 0.0))[0] if c else None
    return out


def _videos(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    best: dict[str, tuple[int, str]] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in conn.execute("SELECT ia.incident_id, ia.artifact_id, a.size FROM incident_artifacts ia JOIN"
                              f" artifacts a ON a.id=ia.artifact_id WHERE ia.incident_id IN ({_in(chunk)}) AND"
                              " a.mime LIKE 'video/%'", chunk):
            key = (r[2] or 0, r[1])
            if r[0] not in best or key < best[r[0]]:
                best[r[0]] = key
    return {k: v[1] for k, v in best.items()}


def _dashboard_heavy(st: Any, conn: sqlite3.Connection, run: str | None, live: bool = True) -> dict[str, Any]:
    from hydra.api_cache import cache_root

    run_row = _run_row(conn, run) if run else None
    members = _membership(conn, run)
    scope = run_incidents(conn, run, live=live)
    incidents = []
    for i in range(0, len(scope), 900):
        chunk = scope[i:i + 900]
        incidents += conn.execute(f"SELECT {INCIDENT_COLS} FROM incidents WHERE id IN ({_in(chunk)})", chunk).fetchall()
    incidents.sort(key=lambda r: r["id"])
    ids = [r["id"] for r in incidents]
    roles = {r[0]: r[1] for r in conn.execute(
        f"SELECT role, COUNT(*) FROM incident_artifacts WHERE incident_id IN ({_in(scope)}) GROUP BY 1", scope)}
    fam: dict[str, dict[str, int]] = defaultdict(dict)
    for r in conn.execute(f"SELECT family, status, COUNT(*) FROM genes WHERE incident_id IN ({_in(scope)})"
                          " GROUP BY 1, 2", scope):
        fam[r[0]][r[1]] = r[2]
    enriched = conn.execute(
        f"SELECT COUNT(DISTINCT incident_id) FROM genes WHERE incident_id IN ({_in(scope)}) AND evidence LIKE"
        " '%enrichment_key%' AND json_extract(evidence, '$.enrichment_key') IS NOT NULL", scope).fetchone()[0]
    groups = _groups(conn, run) if run else []
    camp_rows = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM campaign_members m WHERE m.run_id=c.run_id AND m.campaign_id=c.id)"
        " AS n FROM campaigns c WHERE c.run_id=? ORDER BY n DESC, c.id", (run,)).fetchall() if run else []
    camp_members: dict[str, list[str]] = defaultdict(list)
    if run:
        for m in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id=?"
                              " ORDER BY incident_id", (run,)):
            camp_members[m[0]].append(m[1])
    thumbs = _best_images(conn, ids, st.store,
                          cache_root(st.settings) if hasattr(st, "settings") else None)
    group_of: dict[str, str] = {}
    for g in groups:
        for cid in g.get("campaign_ids") or []:
            group_of.setdefault(cid, g["id"])
    campaigns = []
    for r in camp_rows:
        c = _campaign_row(r)
        core = sorted(c.get("core_genome") or [], key=lambda g: -(g.get("bits") or 0))[:3]
        campaigns.append({"id": c["id"], "label": c.get("label"), "size": r["n"], "confidence": c.get("confidence"),
                          "group_id": c.get("group_id") or group_of.get(c["id"]),
                          "members": [{"id": m, "thumbnail": thumbs.get(m)} for m in camp_members.get(c["id"], [])],
                          "pivots": core})
    order = {c["id"]: i for i, c in enumerate(campaigns)}
    schemes = _first_values(conn, ids, "scheme")
    videos = _videos(conn, ids)
    previews = [{
        "id": r["id"], "title": r["title"], "observed_at": r["observed_at"], "thumbnail": thumbs.get(r["id"]),
        "source": display_source(r["source"], r["source_ref"]), "scheme": schemes.get(r["id"]),
        "campaign_id": members.get(r["id"]),
        "campaign_index": order.get(members.get(r["id"], ""), None), "video": videos.get(r["id"]),
    } for r in incidents]
    metrics = (run_row or {}).get("metrics") or {}
    matrix = _family_matrix(conn, scope)
    coverage = [{"incident_id": i, "families": {f: matrix.get(i, {}).get(f, {}) for f in FAMILY_ORDER}} for i in ids]
    return {
        "run": {"id": run, "created_at": (run_row or {}).get("created_at"),
                "finished_at": (run_row or {}).get("finished_at"),
                "scorer": metrics.get("scorer"), "timings_s": metrics.get("timings_s"),
                "hypotheses": metrics.get("hypotheses"), "candidate_pairs": metrics.get("candidate_pairs"),
                "alerts": metrics.get("alerts") or []} if run else None,
        "kpis": {
            "incidents": len(incidents), "artifacts_by_role": roles, "artifacts": sum(roles.values()),
            "genes_by_family": dict(fam), "genes": sum(sum(v.values()) for v in fam.values()),
            "campaigns": len(campaigns), "multi_member_campaigns": sum(1 for c in campaigns if c["size"] > 1),
            "groups": len(groups), "assignments": 0,
            "enriched_incidents": enriched,
        },
        "previews": previews,
        "campaigns": campaigns,
        "groups": groups,
        "coverage": coverage,
    }


def warm_run(st: Any, conn: sqlite3.Connection, run: str) -> None:
    from hydra import dataflow
    from hydra.api_cache import cached

    if dataflow.ensure(st.settings.db_path, st.writer, conn, run):
        cached(st.settings, conn, "dashboard-base", run, {}, lambda: _dashboard_heavy(st, conn, run, live=False),
               base_only=True)
    else:
        cached(st.settings, conn, "dashboard", run, {}, lambda: _dashboard_heavy(st, conn, run))
    cached(st.settings, conn, "analytics-overview", run, {}, lambda: _analytics_heavy(conn, run))
    # the dashboard graph tile: /v1/graph?min_p=0&min_bits=10&links_only=true (same memo key as the endpoint)
    fp = api_cache.run_fingerprint(conn, run) if api_cache.enabled() and not api_cache.disk_only() else None
    if fp is not None:
        from types import SimpleNamespace

        reviews = _pair_reviews(conn)
        key = ["task-graph", run, fp, api_cache.reviews_digest(reviews), [0.0, None, None, 20000, 10.0, True]]
        api_cache.memo_response(SimpleNamespace(headers={"accept-encoding": "gzip"}), key, lambda: _task_graph_payload(
            conn, run, reviews, 0.0, None, None, 20000, " AND total_bits>=? AND hypothesis!='independent'", [10.0]))


@router.get("/dashboard")
def dashboard(request: Request, run_id: str | None = None) -> Any:
    """Centrum dowodzenia. Per-run part from hydra.api_cache (runs are immutable); jobs and the assignment count
    are read live."""
    from hydra import dataflow
    from hydra.api_cache import cached, json_response

    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        live = dataflow.ensure(st.settings.db_path, st.writer, conn, run)
        if live:  # DATAFLOW: immutable base cached once, new material overlaid from the materialised deltas
            heavy, hit = cached(st.settings, conn, "dashboard-base", run, {},
                                lambda: _dashboard_heavy(st, conn, run, live=False), base_only=True)
            heavy = dataflow.dashboard_view(conn, run, heavy)
        else:
            heavy, hit = cached(st.settings, conn, "dashboard", run, {}, lambda: _dashboard_heavy(st, conn, run))
            heavy = {**heavy, "live": dataflow.live_info(conn, run)}
        jobs = [dict(r) | {"params": _json(r["params"], {})} for r in conn.execute(
            "SELECT id, kind, status, total, done, failed, error_code, params, created_at, finished_at FROM jobs"
            " ORDER BY created_at DESC LIMIT 10")]
        for j in jobs:
            p = {k: (safe_ref(v) if isinstance(v, str) else [safe_ref(x) for x in v] if isinstance(v, list) else v)
                 for k, v in j["params"].items()}
            if p.get("url"):
                p["url"] = _redact_url(p["url"])
            j["params"] = p
        if live:
            return json_response({**heavy, "jobs": jobs}, hit=hit)
        n_assign = len(_latest_assignments(conn, run)) if run else 0
    return json_response({**heavy, "kpis": {**heavy["kpis"], "assignments": n_assign}, "jobs": jobs}, hit=hit)


def _simulation_incident(conn: sqlite3.Connection, run: str, wanted: str | None) -> str | None:
    if wanted and conn.execute("SELECT 1 FROM incidents WHERE id=?", (wanted,)).fetchone():
        return wanted
    # default: the member of the largest campaign with the most observed genes (a rich, linked example)
    row = conn.execute(
        "SELECT m.incident_id FROM campaign_members m JOIN (SELECT campaign_id, COUNT(*) AS n FROM campaign_members"
        " WHERE run_id=? GROUP BY 1) s ON s.campaign_id=m.campaign_id WHERE m.run_id=?"
        " ORDER BY s.n DESC, (SELECT COUNT(*) FROM genes g WHERE g.incident_id=m.incident_id) DESC LIMIT 1",
        (run, run)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT id FROM incidents ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def _extract_stats(conn: sqlite3.Connection, incident_id: str) -> dict[str, Any] | None:
    """Per-incident extraction stats from runs.metrics.extract; the newest run that actually extracted
    (not a cache hit) supplies the seconds."""
    latest, measured = None, None
    for r in conn.execute("SELECT id, metrics FROM runs WHERE kind='analyse' ORDER BY created_at DESC"):
        st = ((_json(r["metrics"], {}) or {}).get("extract") or {}).get(incident_id)
        if not st:
            continue
        latest = latest or {**st, "run_id": r["id"]}
        if not st.get("cached"):
            measured = {**st, "run_id": r["id"]}
            break
    if latest is None:
        return None
    return {**latest, "measured_seconds": (measured or {}).get("seconds"),
            "measured_run_id": (measured or {}).get("run_id")}


def _level(row: sqlite3.Row) -> str:
    """CORE's own evidence level for a gene (hydra.synapse.level): campaign | kit | asset | none."""
    try:
        from types import SimpleNamespace

        from hydra.synapse import level  # type: ignore[attr-defined]

        return level(SimpleNamespace(dependency_group=row["dependency_group"], kind=row["kind"],
                                     family=row["family"]))
    except Exception:  # noqa: BLE001 — older CORE: level unknown, shown as such
        return "unknown"


def _auto_presets(conn: sqlite3.Connection, run: str) -> list[tuple[str, str, str]]:
    """One illustrative pair per outcome, chosen from the run by rule (not by hand):
    the two strongest same-campaign pairs, the strongest abstain, the strongest shared template, and the
    independent pair with the most bits (a trap the scorer did not fall for)."""
    picks: list[tuple[str, str, str]] = []
    q = ("SELECT a, b FROM pairs WHERE run_id=? AND hypothesis=? ORDER BY "
         "COALESCE(json_extract(detail, '$.support'), total_bits) DESC, total_bits DESC LIMIT ?")
    for hyp, n, why in (("same_campaign", 2, "ta sama kampania"), ("abstain", 1, "wstrzymane"),
                        ("shared_template", 1, "wspólny szablon"), ("shared_asset", 1, "wspólny zasób"),
                        ("independent", 1, "pułapka: niezależne mimo bitów")):
        for r in conn.execute(q, (run, hyp, n)):
            picks.append((r["a"], r["b"], why))
    return picks[:5]


@router.get("/simulation")
def simulation(request: Request, run_id: str | None = None, incident: str | None = None,
               presets: str | None = Query(None, description="a:b,c:d (default: picked from the run by rule)")) -> dict:
    """Per-stage aggregates of one real run for the pipeline simulation (8 stages)."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None}
        run_row = _run_row(conn, run) or {}
        metrics = run_row.get("metrics") or {}
        scope = run_incidents(conn, run)
        n_inc = len(scope)

        # 1 import
        items = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM job_items GROUP BY 1")}
        roles = {r[0]: r[1] for r in conn.execute(
            f"SELECT role, COUNT(*) FROM incident_artifacts WHERE incident_id IN ({_in(scope)}) GROUP BY 1", scope)}
        per_inc = [{"id": r["id"], "title": r["title"], "files": r["n"], "thumbnail": None} for r in conn.execute(
            "SELECT i.id, i.title, (SELECT COUNT(*) FROM incident_artifacts ia WHERE ia.incident_id=i.id) AS n"
            f" FROM incidents i WHERE i.id IN ({_in(scope)}) ORDER BY i.id", scope)]
        for p in per_inc:
            p["thumbnail"] = _best_image(conn, p["id"], _st(request).store)
        redacted = conn.execute(f"SELECT COUNT(*), COUNT(DISTINCT incident_id) FROM genes WHERE value LIKE ?"
                                f" AND incident_id IN ({_in(scope)})", ("%[REDACTED]%", *scope)).fetchone()
        stage_import = {"incidents": n_inc, "artifacts_by_role": roles, "job_items_by_status": items,
                        "redacted_genes": redacted[0], "redacted_incidents": redacted[1], "incidents_list": per_inc,
                        "timings_s": metrics.get("timings_s")}

        # 2 genes for one incident
        iid = _simulation_incident(conn, run, incident)
        by_family: dict[str, dict[str, int]] = defaultdict(dict)
        by_extractor: dict[str, int] = Counter()
        for r in conn.execute("SELECT family, status, extractor, COUNT(*) AS n FROM genes WHERE incident_id=?"
                              " GROUP BY 1, 2, 3", (iid,)):
            by_family[r["family"]][r["status"]] = by_family[r["family"]].get(r["status"], 0) + r["n"]
            by_extractor[r["extractor"]] += r["n"]
        creative = _best_image(conn, iid) if iid else None
        boxes = []
        if creative:
            for r in conn.execute(
                "SELECT family, kind, value, json_extract(evidence, '$.bbox') AS bbox FROM genes WHERE incident_id=?"
                " AND json_extract(evidence, '$.artifact_id')=? AND json_extract(evidence, '$.bbox') IS NOT NULL"
                " AND status='observed' LIMIT 300", (iid, creative)):
                bbox = _json(r["bbox"])
                if isinstance(bbox, list) and len(bbox) == 4:
                    boxes.append({"family": r["family"], "kind": r["kind"], "value": r["value"], "bbox": bbox})
        art = conn.execute("SELECT meta FROM artifacts WHERE id=?", (creative,)).fetchone() if creative else None
        title = conn.execute("SELECT title FROM incidents WHERE id=?", (iid,)).fetchone() if iid else None
        stage_genes = {"incident_id": iid, "title": title[0] if title else None,
                       "by_family": {f: by_family.get(f, {}) for f in FAMILY_ORDER},
                       "by_extractor": dict(by_extractor.most_common()), "extract": _extract_stats(conn, iid or ""),
                       "creative": creative, "creative_meta": _json(art["meta"], {}) if art else {}, "boxes": boxes}

        # 3 rarity: document frequency of discrete observed genes, by CORE's evidence level
        df_rows = conn.execute(
            "SELECT family, kind, value, dependency_group, COUNT(DISTINCT incident_id) AS df FROM genes"
            f" WHERE status='observed' AND value IS NOT NULL AND vector_ref IS NULL AND incident_id IN ({_in(scope)})"
            " GROUP BY 1, 2, 3, 4", scope).fetchall()
        hist: dict[int, Counter] = defaultdict(Counter)
        rare, common = [], []
        for r in df_rows:
            lv = _level(r)
            hist[r["df"]][lv] += 1
            if r["df"] >= 2 and lv == "campaign":
                rare.append({"family": r["family"], "kind": r["kind"], "value": r["value"], "df": r["df"], "level": lv})
            if lv == "none" or r["df"] >= max(3, int(0.5 * n_inc)):
                common.append({"family": r["family"], "kind": r["kind"], "value": r["value"], "df": r["df"],
                               "level": lv})
        rare.sort(key=lambda g: (g["df"], g["kind"]))
        common.sort(key=lambda g: (-g["df"], g["kind"]))
        stage_rarity = {"incidents": n_inc, "distinct_values": len(df_rows),
                        "histogram": [{"df": d, **dict(c)} for d, c in sorted(hist.items())],
                        "rare_shared": rare[:12], "boilerplate": common[:12]}

        # 4 candidates for the incident
        cands = []
        src_count: Counter = Counter()
        for r in conn.execute("SELECT a, b, probability, total_bits, hypothesis, detail FROM pairs WHERE run_id=?"
                              " AND (a=? OR b=?) ORDER BY total_bits DESC", (run, iid, iid)):
            srcs = (_json(r["detail"], {}) or {}).get("candidate_sources") or []
            index_names = sorted({s.split(":")[0] for s in srcs})
            for s in index_names:
                src_count[s] += 1
            cands.append({"other": r["b"] if r["a"] == iid else r["a"], "probability": r["probability"],
                          "total_bits": r["total_bits"], "hypothesis": r["hypothesis"], "indexes": index_names,
                          "sources": srcs[:40], "n_sources": len(srcs)})
        stage_candidates = {"incident_id": iid, "pairs": cands, "by_index": dict(src_count),
                            "candidate_pairs_total": metrics.get("candidate_pairs"),
                            "all_pairs": n_inc * (n_inc - 1) // 2}

        # 5 evidence presets
        if presets:
            chosen = [(*sorted(x.split(":", 1)), "wybrane") for x in presets.split(",") if ":" in x][:6]
        else:
            chosen = _auto_presets(conn, run)
        stage_evidence = {"presets": [{"a": a, "b": b, "why": why} for a, b, why in chosen]}

        # 6 decision
        hyp = {r[0]: r[1] for r in conn.execute("SELECT hypothesis, COUNT(*) FROM pairs WHERE run_id=? GROUP BY 1",
                                                (run,))}
        bins = [0] * 10
        points = []
        for r in conn.execute("SELECT a, b, probability, total_bits, hypothesis,"
                              " json_extract(detail, '$.support') AS support FROM pairs WHERE run_id=?", (run,)):
            bins[min(9, int(r["probability"] * 10))] += 1
            points.append({"a": r["a"], "b": r["b"], "p": r["probability"], "bits": r["total_bits"],
                           "support": r["support"], "hypothesis": r["hypothesis"]})
        stage_decision = {"hypotheses": hyp, "probability_bins": bins, "band": [0.2, 0.8], "points": points,
                          "thresholds": (run_row.get("params") or {}).get("thresholds")}

        # 7 grouping
        edges = sorted(points, key=lambda e: -e["p"])
        camps = [{"id": r["id"], "label": r["label"], "members": [m[0] for m in conn.execute(
            "SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=? ORDER BY 1", (run, r["id"]))]}
            for r in conn.execute("SELECT id, label FROM campaigns WHERE run_id=? ORDER BY id", (run,))]
        eval_id = resolve_run(conn, None, "eval")
        eval_row = _run_row(conn, eval_id) if eval_id else None
        em = (eval_row or {}).get("metrics") or {}
        truth = em.get("labels") or em.get("ground_truth") or None  # only labels EVAL chose to publish
        stage_grouping = {"edges": edges, "campaigns": camps, "groups": _groups(conn, run),
                          "truth": truth if isinstance(truth, dict) else None,
                          "truth_source": eval_id if isinstance(truth, dict) else None}

        # 8 result
        stage_result = {"campaigns": len(camps), "multi_member": sum(1 for c in camps if len(c["members"]) > 1),
                        "groups": len(stage_grouping["groups"]),
                        "run_metrics": {k: metrics.get(k) for k in ("incidents", "genes", "pairs_scored",
                                                                    "candidate_pairs", "hypotheses", "scorer",
                                                                    "timings_s")},
                        "eval_run_id": eval_id, "eval_headline": em.get("headline"),
                        "eval_backend": (em.get("meta") or {}).get("backend")}
    return {"run_id": run, "created_at": run_row.get("created_at"),
            "stages": {"import": stage_import, "genes": stage_genes, "rarity": stage_rarity,
                       "candidates": stage_candidates, "evidence": stage_evidence, "decision": stage_decision,
                       "grouping": stage_grouping, "result": stage_result}}


# --------------------------------------------------------------------------------------- AI analyst (optional)
# The LLM layer (hydra.llm, feat/llm) only rephrases evidence the API already computed; it never decides.
# Contract (docs/CONTRACT_REQUESTS.md CR-UI-5): `is_enabled(settings) -> bool`,
# `brief_for_campaign(card: dict, *, settings) -> dict`, `explain_pair(pair: dict, *, settings) -> dict`, each
# returning {"status": "ok", "text_pl": str, "citations": [{"n", "section", "index", "label"}], "model", ...}.

AI_OFF_PL = "Warstwa AI wyłączona — wyniki opierają się wyłącznie na dowodach."


def _llm_module(settings: Any) -> tuple[Any | None, str | None]:
    try:
        from hydra import llm  # type: ignore[attr-defined]
    except ImportError:
        return None, f"{AI_OFF_PL} (moduł hydra.llm nie jest zainstalowany w tej wersji)"
    enabled = getattr(llm, "is_enabled", None) or getattr(llm, "enabled", None)
    try:
        on = bool(enabled(settings)) if callable(enabled) else bool(enabled)
    except Exception:  # noqa: BLE001
        on = False
    if not on:
        return None, AI_OFF_PL
    return llm, None


def _llm_call(request: Request, fn_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = _st(request).settings
    llm, why = _llm_module(settings)
    if llm is None or not hasattr(llm, fn_name):
        return {"status": "unavailable", "reason_pl": why or f"{AI_OFF_PL} (brak funkcji {fn_name})"}
    try:
        out = getattr(llm, fn_name)(payload, settings=settings)
    except Exception as exc:  # noqa: BLE001 — an LLM failure never breaks the evidence views
        return {"status": "error", "reason_pl": f"Warstwa AI zwróciła błąd ({type(exc).__name__}); dowody poniżej są "
                                                "kompletne bez niej."}
    out = out.model_dump(mode="json") if hasattr(out, "model_dump") else dict(out or {})
    out.setdefault("status", "ok")
    out["citations"] = [c for c in out.get("citations") or [] if isinstance(c, dict)]
    return json.loads(json.dumps(out, default=str))


@router.get("/campaigns/{campaign_id}/brief")
def campaign_brief(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        card = _campaign_card(conn, run, campaign_id)
    return _llm_call(request, "brief_for_campaign", card)


@router.get("/pairs/{a}/{b}/explain")
def pair_explain(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    pair = get_pair(request, a, b, run_id)
    return _llm_call(request, "explain_pair", pair)


# --------------------------------------------------------------------------------------- analytics (redesign)
# Aggregates for the dashboard charts. Units are stated per block; nothing is estimated.

TIER_KEYS = ("operator_bits", "campaign_bits", "kit_bits", "asset_bits")
INFRA_PANEL_KINDS = ("registrar", "ns_set", "asn", "as_org", "cert_pl_listed_host")
INDICATOR_KINDS = ("tracker_id", "landing_host", "registrable_domain", "asset_sha256", "entity_phone",
                   "ns_set", "registrar", "favicon_mmh3", "persona_renamed", "person_named", "as_org", "asn",
                   "cert_issuer", "ns_host")


def _analytics_heavy(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Per-run analytics (cached by hydra.api_cache). Reads only the pair JSON it needs: intra-campaign pairs for
    the Sankey (joined through campaign_members, primary-key lookups) and the 40 strongest links for the tiers."""
    import orjson

    def loads(text: str | None) -> dict[str, Any]:
        try:
            d = orjson.loads(text) if text else {}
        except orjson.JSONDecodeError:
            return {}
        return d if isinstance(d, dict) else {}

    members = _membership(conn, run)
    camps = {r["id"]: {"id": r["id"], "label": r["label"], "members": []} for r in conn.execute(
        "SELECT id, label FROM campaigns WHERE run_id=? ORDER BY id", (run,))}
    for iid, cid in members.items():
        if cid in camps:
            camps[cid]["members"].append(iid)
    groups = _groups(conn, run)
    group_of = {cid: g["id"] for g in groups for cid in (g.get("campaign_ids") or [])}
    scope = run_incidents(conn, run)

    sankey: dict[tuple[str, str], float] = defaultdict(float)
    for r in conn.execute(
        "SELECT p.a, p.b, p.detail FROM campaign_members ma JOIN campaign_members mb ON mb.run_id=ma.run_id"
        " AND mb.campaign_id=ma.campaign_id AND mb.incident_id>ma.incident_id"
        " JOIN pairs p ON p.run_id=ma.run_id AND p.a=ma.incident_id AND p.b=mb.incident_id WHERE ma.run_id=?",
        (run,)):
        ca = members.get(r[0])
        if not ca or ca != members.get(r[1]):
            continue
        for fam, bits in (loads(r[2]).get("family_bits") or {}).items():
            if bits and bits > 0:
                sankey[(fam, ca)] += float(bits)
    tiers = []
    top = conn.execute(
        "SELECT a, b, total_bits, hypothesis, detail FROM pairs WHERE rowid IN (SELECT rowid FROM pairs WHERE"
        " run_id=? AND hypothesis IN ('same_campaign','shared_template','shared_asset','abstain')"
        " ORDER BY total_bits DESC LIMIT 40)", (run,)).fetchall()
    for r in sorted(top, key=lambda r: -r["total_bits"]):
        ca, cb = members.get(r["a"]), members.get(r["b"])
        d = loads(r["detail"])
        tiers.append({"a": r["a"], "b": r["b"], "hypothesis": r["hypothesis"], "total_bits": r["total_bits"],
                      "campaign_id": ca if ca == cb else None, **{k: d.get(k) for k in TIER_KEYS},
                      "robustness": (d.get("meta") or {}).get("robustness") or d.get("robustness")})
    timeline = [{"id": r["id"], "title": r["title"], "observed_at": r["observed_at"],
                 "campaign_id": members.get(r["id"])}
                for r in conn.execute(f"SELECT id, title, observed_at FROM incidents WHERE id IN ({_in(scope)})"
                                      " ORDER BY observed_at, id", scope)]
    infra: dict[str, list[dict[str, Any]]] = {}
    for kind in INFRA_PANEL_KINDS:
        rows = conn.execute(
            "SELECT value, COUNT(DISTINCT incident_id) AS n, GROUP_CONCAT(DISTINCT incident_id) AS ids FROM genes"
            f" WHERE kind=? AND status='observed' AND value IS NOT NULL AND incident_id IN ({_in(scope)})"
            " GROUP BY value ORDER BY n DESC, value LIMIT 8", (kind, *scope)).fetchall()
        if rows:
            infra[kind] = [{"value": x["value"], "incidents": sorted((x["ids"] or "").split(",")),
                            "df": x["n"]} for x in rows]
    return {
        "run_id": run,
        "sankey": {"unit": "bits", "links": [{"family": f, "campaign_id": c, "bits": round(v, 2)}
                                             for (f, c), v in sorted(sankey.items())]},
        "tiers": {"unit": "bits", "keys": list(TIER_KEYS), "links": tiers},
        "timeline": timeline,
        "infrastructure": infra,
        "clusters": [{**c, "size": len(c["members"]), "group_id": group_of.get(c["id"])}
                     for c in sorted(camps.values(), key=lambda c: (-len(c["members"]), c["id"]))],
        "groups": groups,
    }


@router.get("/analytics/overview")
def analytics_overview(request: Request, run_id: str | None = None) -> Any:
    """Sankey (family → campaign, bits of internal links), per-link evidence tiers, timeline, infrastructure,
    campaign clusters with groups. All from the latest analyse run (cached per run, hydra.api_cache)."""
    from hydra.api_cache import cached, json_response

    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None}
        data, hit = cached(st.settings, conn, "analytics-overview", run, {}, lambda: _analytics_heavy(conn, run))
    return json_response(data, hit=hit)


@router.get("/analytics/indicators")
def analytics_indicators(request: Request, min_df: int = Query(2, ge=2, le=1000),
                         kinds: str | None = None, limit: int = Query(200, ge=1, le=2000),
                         run_id: str | None = None) -> dict[str, Any]:
    """Bipartite pivot: shared indicator values (pixel IDs, hosts, assets, phones, ...) ↔ incidents carrying them,
    within the selected run (CR-ASTRA-PIPE-304; latest when no run is given)."""
    wanted = tuple(k for k in (kinds.split(",") if kinds else INDICATOR_KINDS) if k)
    marks = ",".join("?" * len(wanted))
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        members = _membership(conn, run)
        scope = run_incidents(conn, run)
        rows = conn.execute(
            f"SELECT MIN(family) AS family, kind, value, COUNT(DISTINCT incident_id) AS df,"
            f" GROUP_CONCAT(DISTINCT incident_id) AS ids, MIN(dependency_group) AS dependency_group,"
            f" MAX(CASE WHEN extractor LIKE 'inf_enrich%' OR extractor LIKE 'enrich%' OR provenance != 'OBS'"
            f" THEN 1 ELSE 0 END) AS inferred"
            f" FROM genes WHERE kind IN ({marks}) AND status='observed' AND value IS NOT NULL"
            f" AND incident_id IN ({_in(scope)}) GROUP BY kind, value"
            f" HAVING df >= ? ORDER BY df DESC, kind LIMIT ?", (*wanted, *scope, min_df, limit)).fetchall()
        n_inc = len(scope)
    indicators = []
    for r in rows:
        level, tier = _gene_level(r)
        indicators.append({"id": f"{r['kind']}:{r['value']}", "family": r["family"], "kind": r["kind"],
                           "value": r["value"], "df": r["df"], "incidents": sorted((r["ids"] or "").split(",")),
                           "level": level, "tier": tier, "inferred": bool(r["inferred"]),
                           # boilerplate tier = values CORE down-weights (incl. MISP warninglist overrides)
                           "over_correlating": level == "none" or tier in ("boilerplate", "label")})
    used = sorted({i for ind in indicators for i in ind["incidents"]})
    return {"run_id": run, "incidents_total": n_inc, "kinds": list(wanted), "indicators": indicators,
            "incidents": [{"id": i, "campaign_id": members.get(i)} for i in used]}


# --------------------------------------------------------------------------------------- one-click demo + corpora
# Known corpora (only those present on this machine are offered). A run's corpus comes from runs.params.corpus
# when CORE records it (CR-UI-6), else from runs.params.paths.

NATIVE_SOURCES = {"nask-sample", "nask", "upload", "url", "screenshot", "file", "path", "api", "hint", "synthetic"}

CORPORA = (
    ("nask", "Próbka NASK", ("nask/sample",), "nask"),
    ("extended", "Rozszerzony korpus (zewnętrzne zbiory)", ("external-big/corpus-demo", "corpora/extended"),
     "extended"),
)
PUBLIC_CORPUS_DIR = "public-corpus"  # adapter folders; run specs `public` and `public:<source>` (see _corpus_dirs)


def _count_incident_dirs(p: Path) -> int:
    return sum(1 for x in p.iterdir() if x.is_dir() and not x.name.startswith("."))


PUBLIC_SOURCE_LABELS = {"certpl": "CERT PL", "phishtank": "PhishTank", "phiusiil": "PhiUSIIL"}


def _public_label(name: str) -> str:
    return PUBLIC_SOURCE_LABELS.get(name.lower(), "CERT PL" if "cert" in name.lower() else name)


def _corpus_dirs(settings: Any, conn: Any = None) -> list[dict[str, Any]]:
    """One-click corpora. NASK / extended from their folders. The public corpus is offered as spec `public` (every
    `public:*` source) and, per source stored in the DB, as `public:<source>`: the adapters under
    `<data>/public-corpus/<folder>` write incidents with `source = public:<source>` (certpl, phishtank, phiusiil),
    which need not equal the folder name (a `pl-feeds` folder holds three sources), so a folder-named spec
    analysed 0 incidents. `incidents_in_db` counts stored incidents of the spec (hydra.corpus), not folders."""
    from hydra.corpus import corpus_counts

    counts = corpus_counts(conn) if conn is not None else {}
    data = Path(settings.data_dir)
    out = []
    for cid, label, rels, spec in CORPORA:
        for rel in rels:
            p = data / rel
            if p.is_dir():
                out.append({"id": cid, "label": label, "spec": spec, "path": safe_ref(str(p)),
                            "incidents_on_disk": _count_incident_dirs(p), "_path": p})
                break
    pub = data / PUBLIC_CORPUS_DIR
    if pub.is_dir():
        folders = sorted(x for x in pub.iterdir() if x.is_dir() and not x.name.startswith("."))
        sources = sorted(c.removeprefix("public:") for c in counts if c.startswith("public:"))
        names = ", ".join(_public_label(x) for x in sources) or ", ".join(x.name for x in folders)
        out.append({"id": "public", "label": "Publiczny korpus" + (f" ({names})" if names else ""), "spec": "public",
                    "path": safe_ref(str(pub)), "incidents_on_disk": sum(_count_incident_dirs(x) for x in folders),
                    "incidents_in_db": sum(n for c, n in counts.items() if c.startswith("public:")), "_path": pub})
        by_name = {x.name: x for x in folders}
        for name in sources:
            folder = by_name.get(name)  # a folder named like its source: import only that folder
            out.append({"id": f"public:{name}", "label": f"Publiczny korpus {_public_label(name)}",
                        "spec": f"public:{name}", "path": safe_ref(str(folder or pub)),
                        "incidents_on_disk": _count_incident_dirs(folder) if folder else None,
                        "incidents_in_db": counts[f"public:{name}"], "_path": folder or pub})
    return out


NATIVE_CORPORA = {"nask", "extended", "public", "all", "mixed", "upload"}


UPLOAD_JOB_SOURCES = {"upload", "url", "screenshot"}
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def display_source(source: str | None, source_ref: str | None) -> str | None:
    """Incident source as shown by the API (audit B16). The NASK folder adapter tags every folder it imports
    `nask-sample`, also a dataset uploaded in the panel (unpacked into the server scratch dir, so its stored
    `source_ref` is relative: `zip-<sha8>/...`, `analyse-<x>/...`); such incidents are shown as `upload`. Local
    organiser folders keep their absolute `source_ref` and stay `nask-sample`. Read-time mapping only: the stored
    row, the incident id and the corpus rules (`hydra.corpus`, upload belongs to the NASK mode) are unchanged."""
    if source == "nask-sample" and source_ref and not source_ref.startswith("/") and not _WIN_ABS.match(source_ref):
        return "upload"
    return source


def corpus_params(params: dict[str, Any], conn: sqlite3.Connection | None) -> dict[str, Any]:
    """The params that carry a run's corpus. A review revision run (`params.root_run` / `revision_of`, written by
    hydra.review) has neither `corpus` nor `paths`: its corpus, and its synthetic flag, are those of the root
    batch run it recomputes (audit B13: the graph said "korpus: unknown" after a review)."""
    seen = 0
    while conn is not None and not params.get("corpus") and not params.get("paths") and seen < 8:
        parent = params.get("root_run") or params.get("revision_of")
        if not parent:
            break
        row = conn.execute("SELECT params FROM runs WHERE id=?", (str(parent),)).fetchone()
        if row is None:
            break
        params = {**(_json(row["params"], {}) or {}), "synthetic": params.get("synthetic")
                  or (_json(row["params"], {}) or {}).get("synthetic")}
        seen += 1
    return params


def run_corpus(params: dict[str, Any], conn: sqlite3.Connection | None = None) -> str:
    """Corpus of a run. `params.corpus` (a hydra.corpus spec) is kept as is when it is a native name (`nask`,
    `extended`, `public`, `all`, `public:<x>`, `ext:<x>`, comma lists of those); any other bare name is an external
    dataset `ext:<name>`. Without a spec: 'nask' | 'ext:<folder>' | 'mixed' | 'unknown' from params.paths.
    With `conn`, a review revision run resolves to the corpus of its root run (`corpus_params`)."""
    params = corpus_params(params, conn)
    if conn is not None and params.get("job_id") and str(params.get("corpus") or "nask") == "nask":
        # audit B16: a dataset uploaded in the panel runs in the NASK mode (spec `nask` = nask + upload), but it is
        # the analyst's material, not the organiser sample: label it `upload` ("Tylko dane NASK" was wrong)
        job = conn.execute("SELECT params FROM jobs WHERE id=?", (str(params["job_id"]),)).fetchone()
        if job is not None and (_json(job["params"], {}) or {}).get("source") in UPLOAD_JOB_SOURCES:
            return "upload"
    if params.get("corpus"):
        c = str(params["corpus"])
        parts = [x.strip() for x in c.split(",") if x.strip()]
        native = all(x in NATIVE_CORPORA or x.startswith(("ext:", "public:")) for x in parts)
        return c if parts and native else f"ext:{c}"
    paths = [str(p) for p in params.get("paths") or []]
    if not paths:
        return "unknown"
    kinds = {"nask" if "/nask/" in p or p.rstrip("/").endswith("/nask") else f"ext:{Path(p).name}" for p in paths}
    return kinds.pop() if len(kinds) == 1 else "mixed"


@router.get("/demo/corpora")
def demo_corpora(request: Request) -> dict[str, Any]:
    st = _st(request)
    with st.reader() as conn:
        items = _corpus_dirs(st.settings, conn)
    return {"items": [{k: v for k, v in c.items() if k != "_path"} for c in items]}


class DemoRun(BaseModel):
    corpus: str = "nask"
    fresh: bool = False  # re-extract genes (no cache): the live view then visibly streams


@router.post("/demo/run", status_code=status.HTTP_202_ACCEPTED)
def demo_run(request: Request, body: DemoRun) -> dict[str, Any]:
    st = _st(request)
    with st.reader() as conn:
        found = next((c for c in _corpus_dirs(st.settings, conn) if c["id"] == body.corpus), None)
    if found is None:
        raise HTTPException(status_code=404, detail="corpus not available on this machine")
    job_id = _submit_analyse(st, input_kind="corpus", path=found["_path"],
                             params={"source": "demo", "mode": "corpus", "corpus": body.corpus,
                                     "spec": found["spec"], "fresh": body.fresh, "path": found["path"]},
                             corpus_kwargs={"corpus": found["spec"], "use_cache": not body.fresh})
    return {"job_id": job_id, "status": "queued", "corpus": body.corpus}


@router.get("/corpora")
def corpora_runs(request: Request) -> dict[str, Any]:
    """Analyse runs grouped by corpus (for the corpus switch); newest run per corpus first."""
    with _st(request).reader() as conn:
        rows = conn.execute("SELECT id, params, created_at, finished_at FROM runs WHERE kind='analyse'"
                            " ORDER BY created_at DESC").fetchall()
        corpus_of = {r["id"]: run_corpus(_json(r["params"], {}) or {}, conn) for r in rows}
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[corpus_of[r["id"]]].append({"id": r["id"], "created_at": r["created_at"],
                                                              "finished_at": r["finished_at"]})
    return {"items": [{"corpus": k, "latest_run": v[0]["id"], "runs": v[:10]} for k, v in by.items()]}


@router.get("/demo/progress/{job_id}")
def demo_progress(request: Request, job_id: str) -> dict[str, Any]:
    """Live corpus view: CORE's run_progress(job_id) when available, else rows written after the job started."""
    st = _st(request)
    with st.reader() as conn:
        job = get_job(conn, job_id)
        if job is None or job["kind"] != "analyse":
            raise HTTPException(status_code=404, detail="analyse job not found")
        job["params"] = {k: safe_ref(v) if isinstance(v, str) else v for k, v in (job.get("params") or {}).items()}
        core = None
        try:
            try:
                from hydra.progress import run_progress as fn  # type: ignore[attr-defined]
            except ImportError:
                from hydra import pipeline  # type: ignore[attr-defined]

                fn = getattr(pipeline, "run_progress", None)
            core = json.loads(json.dumps(fn(job_id, st.settings), default=str)) if fn else None
        except Exception:  # noqa: BLE001 — progress is best effort; tables below are always there
            core = None
        started = job.get("started_at") or job["created_at"]
        run = conn.execute("SELECT id FROM runs WHERE kind='analyse' AND (json_extract(params, '$.job_id')=?"
                           " OR created_at >= ?) ORDER BY json_extract(params, '$.job_id')=? DESC, created_at DESC"
                           " LIMIT 1", (job_id, started, job_id)).fetchone()
        run_id = run[0] if run else None
        if core and core.get("run_id") and not conn.execute(
                "SELECT 1 FROM runs WHERE id=?", (core["run_id"],)).fetchone():
            run_id = None  # CORE announced a run id that is not persisted yet
        fam: dict[str, int] = {r[0]: r[1] for r in conn.execute(
            "SELECT family, COUNT(*) FROM genes WHERE created_at >= ? GROUP BY 1", (started,))}
        new_inc = [r[0] for r in conn.execute("SELECT id FROM incidents WHERE created_at >= ? ORDER BY created_at, id",
                                              (started,))]
        out: dict[str, Any] = {"job": job, "core_progress": core, "run_id": run_id, "new_incidents": new_inc,
                               "genes_written_by_family": fam, "edges": [], "campaigns": []}
        if run_id:
            out["edges"] = [{"a": r["a"], "b": r["b"], "p": r["probability"], "bits": r["total_bits"],
                             "hypothesis": r["hypothesis"],
                             "dominant_family": _dominant_family((_json(r["detail"], {}) or {}).get("family_bits", {}))}
                            for r in conn.execute(
                "SELECT a, b, probability, total_bits, hypothesis, detail FROM pairs WHERE run_id=?"
                " ORDER BY total_bits DESC", (run_id,))]
            m = _membership(conn, run_id)
            out["campaigns"] = [{"id": c, "members": sorted(i for i, x in m.items() if x == c)}
                                for c in sorted(set(m.values()))]
            out["groups"] = _groups(conn, run_id)
        out["incidents"] = [{"id": r["id"], "title": r["title"], "source": r["source"],
                             "thumbnail": _best_image(conn, r["id"], st.store)}
                            for r in conn.execute("SELECT id, title, source FROM incidents ORDER BY id")]
    return out


# --------------------------------------------------------------------------------------- live analysis: explain-data
# Panels for the full-screen analysis of one new item, built from stored rows. Durations come from row
# timestamps (genes.created_at, runs.created_at, jobs.*) and are labelled as such; nothing is estimated.


def _ts(v: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v)).timestamp() if v else None
    except ValueError:
        return None


def _gene_level(row: Any) -> tuple[str, str | None]:
    try:
        from types import SimpleNamespace

        from hydra.synapse import gene_tier, level  # type: ignore[attr-defined]

        g = SimpleNamespace(dependency_group=row["dependency_group"], kind=row["kind"], family=row["family"])
        return level(g), gene_tier(g)
    except Exception:  # noqa: BLE001
        return "unknown", None


def _pl1(v: float) -> str:
    """One decimal with a Polish decimal comma (UI strings)."""
    return f"{float(v):.1f}".replace(".", ",")


def _why_gene(level: str, df_other: int, n_inc: int) -> str:
    share = f"{round(100 * df_other / max(1, n_inc))}%"
    if level == "none":
        return f"szum / boilerplate: 0 b (występuje w {df_other} z {n_inc} materiałów, {share})"
    if df_other == 0:
        return "unikalny dla tego materiału: niczego nie łączy"
    if level == "kit":
        return f"szablon / kit: wspiera grupę kampanii, nie kampanię (w {df_other} innych incydentach)"
    if level == "asset":
        return f"wspólny zasób: zasób może być skopiowany (w {df_other} innych incydentach)"
    return f"rzadki wspólny ślad operatora: występuje w {df_other} innych incydentach"


@router.get("/analyse/{job_id}/explain-data")
def analyse_explain_data(request: Request, job_id: str) -> dict[str, Any]:
    st = _st(request)
    with st.reader() as conn:
        job = get_job(conn, job_id)
        if job is None or job["kind"] != "analyse":
            raise HTTPException(status_code=404, detail="analyse job not found")
        ev = conn.execute("SELECT payload FROM events WHERE type=? AND json_extract(payload, '$.job_id')=?"
                          " ORDER BY seq DESC LIMIT 1", (ANALYSE_EVENT, job_id)).fetchone()
        payload = (_json(ev["payload"], {}) or {}) if ev else {}
        assignment = payload.get("assignment") or {}
        result = payload.get("result") or {}
        live = _live_progress(conn, job, assignment, result, st.store)
        iid = live.get("incident_id")
        out: dict[str, Any] = {"job_id": job_id, "status": job["status"], "incident_id": iid, "ready": bool(iid)}
        if not iid:
            return out
        base = resolve_run(conn)
        n_inc = conn.execute("SELECT COUNT(*) FROM incidents WHERE id != ?", (iid,)).fetchone()[0]

        # ---- stage timeline from timestamps
        started, finished = _ts(job.get("started_at")), _ts(job.get("finished_at"))
        inc_row = conn.execute("SELECT created_at, observed_at, title, source FROM incidents WHERE id=?",
                               (iid,)).fetchone()
        g_rows = conn.execute("SELECT family, extractor, created_at FROM genes WHERE incident_id=?", (iid,)).fetchall()
        times = [t for t in (_ts(r["created_at"]) for r in g_rows) if t]
        windows: dict[str, list[float]] = defaultdict(list)
        ex_windows: dict[str, list[float]] = defaultdict(list)
        for r in g_rows:
            t = _ts(r["created_at"])
            if t:
                windows[r["family"]].append(t)
                ex_windows[r["extractor"]].append(t)
        enrich_t = [t for r in g_rows
                    if str(r["extractor"]).startswith("inf_enrich") and (t := _ts(r["created_at"]))]
        run_row = None
        if live.get("assign_run_id"):
            run_row = conn.execute("SELECT created_at, params FROM runs WHERE id=?",
                                   (live["assign_run_id"],)).fetchone()
        run_t = _ts(run_row["created_at"]) if run_row else None
        first_gene, last_gene = (min(times), max(times)) if times else (None, None)

        def span(a: float | None, b: float | None) -> float | None:
            return round(b - a, 3) if a is not None and b is not None and b >= a else None

        def window(v: list[float]) -> float | None:
            # rows written in one transaction share a timestamp: the split is not measurable, not "0 s"
            w = span(min(v), max(v)) if v else None
            return w if w is not None and w >= 0.01 else None

        one_tx = first_gene is not None and last_gene is not None and last_gene - first_gene < 0.01
        stages = [
            {"key": "import", "label": "import + ekstrakcja genów" if one_tx else "import + artefakty",
             "seconds": span(started, first_gene or run_t),
             "state": "done" if first_gene or run_t else ("running" if job["status"] == "running" else "waiting")},
            {"key": "genes", "label": "zapis genów (jedna transakcja)" if one_tx else "geny",
             "seconds": None if one_tx else span(first_gene, last_gene),
             "state": "done" if run_t else ("running" if first_gene else "waiting")},
            {"key": "enrich", "label": "wzbogacanie",
             "seconds": window(enrich_t),
             "state": "done" if enrich_t and run_t else ("running" if first_gene and not run_t else
                                                          ("skipped" if run_t else "waiting"))},
            {"key": "assign", "label": "kandydaci + ocena + decyzja", "seconds": span(last_gene, run_t),
             "state": "done" if run_t else ("running" if last_gene else "waiting")},
        ]
        per_family_s = {f: window(v) for f, v in windows.items()}
        per_extractor = sorted(({"extractor": e, "genes": len(v), "seconds": window(v)}
                                for e, v in ex_windows.items()), key=lambda x: -x["genes"])

        # ---- artefacts
        arts = []
        for r in conn.execute(
            "SELECT ia.artifact_id, ia.role, ia.path_in_incident, a.mime, a.size FROM incident_artifacts ia"
            " LEFT JOIN artifacts a ON a.id=ia.artifact_id WHERE ia.incident_id=?"
            " ORDER BY ia.role, ia.path_in_incident",
            (iid,)
        ):
            others = [x[0] for x in conn.execute(
                "SELECT DISTINCT incident_id FROM incident_artifacts WHERE artifact_id=? AND incident_id != ?",
                (r["artifact_id"], iid))]
            red = conn.execute("SELECT COUNT(*) FROM genes WHERE incident_id=? AND value LIKE ? AND"
                               " json_extract(evidence, '$.artifact_id')=?",
                               (iid, "%[REDACTED]%", r["artifact_id"])).fetchone()[0]
            arts.append({"artifact_id": r["artifact_id"], "sha_short": r["artifact_id"][:12], "role": r["role"],
                         "path": r["path_in_incident"], "mime": r["mime"], "size": r["size"],
                         "duplicate_of": others, "redactions": red,
                         "previewable": bool(r["mime"]) and str(r["mime"]).startswith(("image/", "video/"))
                         and r["mime"] != "image/svg+xml"})

        # ---- gene feed (most informative first)
        best = next((c for c in assignment.get("candidates") or []
                     if c.get("campaign_id") == assignment.get("campaign_id")), None) or \
            ((assignment.get("candidates") or [None])[0])
        bp = (best or {}).get("best_pair") or {}
        members_base = _membership(conn, base) if base else {}
        run_details: dict[str, dict[str, Any]] = {}
        if live.get("assign_run_id"):
            for r in conn.execute("SELECT a, b, total_bits, detail FROM pairs WHERE run_id=? ORDER BY total_bits DESC",
                                  (live["assign_run_id"],)):
                other = r["b"] if r["a"] == iid else r["a"]
                camp = members_base.get(other)
                if camp and camp not in run_details:  # strongest pair per candidate campaign
                    run_details[camp] = {**(_json(r["detail"], {}) or {}), "a": r["a"], "b": r["b"],
                                         "total_bits": r["total_bits"]}
        chosen = run_details.get((best or {}).get("campaign_id") or "", {})
        reason_bits = {(m.get("kind"), m.get("value_a") or m.get("value_b")): m.get("bits")
                       for m in bp.get("reasons") or []}
        if not reason_bits and chosen.get("reasons"):
            reason_bits = {(m.get("kind"), m.get("value_a") or m.get("value_b")): m.get("bits")
                           for m in chosen["reasons"]}
        feed = []
        for r in conn.execute(
            "SELECT g.family, g.kind, g.value, g.dependency_group, g.provenance,"
            " (SELECT COUNT(DISTINCT h.incident_id) FROM genes h WHERE h.kind=g.kind AND h.value=g.value"
            "  AND h.incident_id != g.incident_id AND h.status='observed') AS df_other"
            " FROM genes g WHERE g.incident_id=? AND g.status='observed' AND g.value IS NOT NULL"
            " AND g.vector_ref IS NULL GROUP BY g.kind, g.value LIMIT 3000", (iid,)
        ):
            lv, tier = _gene_level(r)
            bits = reason_bits.get((r["kind"], r["value"]))
            feed.append({"family": r["family"], "kind": r["kind"], "value": r["value"], "provenance": r["provenance"],
                         "df_other": r["df_other"], "level": lv, "tier": tier, "bits": bits,
                         "why": _why_gene(lv, r["df_other"], n_inc)})
        rank = {"campaign": 0, "asset": 1, "kit": 2, "unknown": 3, "none": 4}
        feed.sort(key=lambda g: (g["bits"] is None, -(g["bits"] or 0), g["df_other"] == 0,
                                 rank.get(g["level"], 3), g["df_other"], g["kind"]))
        rare = sum(1 for g in feed if g["df_other"] > 0 and g["level"] == "campaign")

        # ---- enrichment facts
        enr = []
        for r in conn.execute(
            "SELECT kind, value, extractor, evidence, created_at FROM genes WHERE incident_id=? AND"
            " json_extract(evidence, '$.enrichment_key') IS NOT NULL ORDER BY kind", (iid,)
        ):
            e = _json(r["evidence"], {}) or {}
            rec = conn.execute("SELECT source, fetched_at, status FROM enrichment WHERE key=?",
                               (e.get("enrichment_key"),)).fetchone()
            enr.append({"kind": r["kind"], "value": r["value"],
                        "subject": str(e.get("note") or "").split(":")[0] or None,
                        "source": rec["source"] if rec else r["extractor"],
                        "fetched_at": rec["fetched_at"] if rec else None,
                        "origin": "rekord wzbogacenia" if rec else "z genów (brak rekordu z datą pobrania)"})

        # ---- candidates + funnel
        idx: Counter = Counter()
        pairs = []
        if live.get("assign_run_id"):
            for r in conn.execute("SELECT a, b, probability, total_bits, hypothesis, detail FROM pairs WHERE run_id=?"
                                  " ORDER BY total_bits DESC", (live["assign_run_id"],)):
                d = _json(r["detail"], {}) or {}
                names = sorted({str(s).split(":")[0] for s in d.get("candidate_sources") or []})
                for n in names:
                    idx[n] += 1
                pairs.append({"a": r["a"], "b": r["b"], "other": r["b"] if r["a"] == iid else r["a"],
                              "bits": r["total_bits"], "p": r["probability"], "hypothesis": r["hypothesis"],
                              "indexes": names, "dominant_family": _dominant_family(d.get("family_bits", {}))})
        funnel = {"incidents": n_inc, "candidates": len(pairs), "scored": len(pairs),
                  "linked": sum(1 for p in pairs if p["hypothesis"] == "same_campaign")}

        # ---- why: top-3 candidate campaigns
        th = ((_json(run_row["params"], {}) if run_row else {}) or {}).get("thresholds") or \
            ((_run_row(conn, base) or {}).get("params", {}) or {}).get("thresholds") or {}
        top = []
        for c in (assignment.get("candidates") or [])[:3]:
            p = {**(c.get("best_pair") or {}), **run_details.get(c.get("campaign_id") or "", {})}
            top.append({"campaign_id": c.get("campaign_id"), "probability": c.get("probability"),
                        "support": p.get("support"), "members_supporting": c.get("support"),
                        "total_bits": p.get("total_bits"),
                        "family_bits": p.get("family_bits") or {},
                        "campaign_by_family": p.get("campaign_by_family") or {},
                        "campaign_bits": p.get("campaign_bits"), "kit_bits": p.get("kit_bits"),
                        "asset_bits": p.get("asset_bits"), "pair": [p.get("a"), p.get("b")] if p.get("a") else None})
        sup = [t["support"] if isinstance(t["support"], int | float) else (t["total_bits"] or 0) for t in top]
        margin = round(sup[0] - sup[1], 2) if len(sup) >= 2 else None
        link = th.get("link")
        rob = chosen.get("robustness") or bp.get("robustness") or (bp.get("meta") or {}).get("robustness")
        if chosen.get("a"):
            bp = {**bp, "a": chosen["a"], "b": chosen["b"]}
        dec = assignment.get("decision")
        if dec == "assign":
            crit = (rob or {}).get("critical_families") or []
            counter = ("decyzja utrzymałaby się bez każdej pojedynczej rodziny" if rob and rob.get("robust_link")
                       else f"decyzja zależy od rodzin: {', '.join(crit)}" if crit else None)
        elif sup and link is not None:
            counter = f"brakuje {_pl1(max(0.0, link - sup[0]))} b do progu kampanii ({_pl1(link)} b)"
        else:
            counter = None
        reasons = [m for m in bp.get("reasons") or [] if m.get("kind") != "cap_correction"][:3]
        rationale = []
        if dec:
            in_group = f" w grupie {assignment.get('group_id')}." if assignment.get("group_id") else "."
            lead = {"assign": f"Materiał przypisano do kampanii {assignment.get('campaign_id')}.",
                    "new_campaign": "Żadna znana kampania nie ma wystarczającego poparcia: nowy wzorzec" + in_group,
                    "abstain": "Dowody są niejednoznaczne: decyzja dla analityka."}.get(dec, "")
            rationale.append(lead)
        rationale += [f"{m.get('explanation_pl')}" for m in reasons if m.get("explanation_pl")]
        if margin is not None:
            rationale.append(f"Przewaga nad drugą kampanią: {_pl1(margin)} b.")
        if counter:
            rationale.append(counter[0].upper() + counter[1:] + ".")

        # ---- metrics, profile, graph, timeline
        fam_status: dict[str, dict[str, int]] = defaultdict(dict)
        for r in conn.execute("SELECT family, status, COUNT(*) FROM genes WHERE incident_id=? GROUP BY 1, 2", (iid,)):
            fam_status[r[0]][r[1]] = r[2]
        core = []
        cid = assignment.get("campaign_id") or (best or {}).get("campaign_id")
        if cid and base:
            c = conn.execute("SELECT detail FROM campaigns WHERE run_id=? AND id=?", (base, cid)).fetchone()
            core = ((_json(c["detail"], {}) or {}).get("core_genome") or []) if c else []
        new_profile = Counter(g["family"] for g in feed if g["df_other"] > 0 and g["level"] in ("campaign", "asset"))
        camp_profile = Counter(str(g.get("family")) for g in core)
        members = live.get("campaign_members") or []
        tl = []
        for m in [iid, *[x["id"] for x in members]]:
            r = conn.execute("SELECT observed_at FROM incidents WHERE id=?", (m,)).fetchone()
            tl.append({"id": m, "observed_at": r[0] if r else None, "is_new": m == iid})
        out.update({
            "title": inc_row["title"] if inc_row else None, "source": inc_row["source"] if inc_row else None,
            "thumbnail": live.get("thumbnail"),
            "timing_source": "znaczniki czasu wierszy (genes.created_at, runs.created_at, jobs)"
                             + ("; geny zapisane jedną transakcją: czasów rodzin i ekstraktorów"
                                " nie da się rozdzielić" if one_tx else ""),
            "stages": stages, "per_family_seconds": per_family_s, "per_extractor": per_extractor[:20],
            "total_seconds": span(started, finished),
            "artifacts": arts, "skipped": [],
            "genes_feed": feed[:40], "genes_total": len(g_rows), "rare_genes": rare,
            "enrichment": enr[:30],
            "candidates": {"by_index": dict(idx), "pairs": pairs[:12], "funnel": funnel},
            "why": {"top": top, "margin": margin, "link_threshold": link, "thresholds": th,
                    "best_pair": bp.get("a") and [bp.get("a"), bp.get("b")], "counterfactual": counter,
                    "rationale_pl": rationale, "robustness": rob, "conformal": assignment.get("conformal")},
            "decision": {"decision": dec, "campaign_id": assignment.get("campaign_id"),
                         "group_id": assignment.get("group_id"), "reason_pl": assignment.get("reason_pl"),
                         "probability": (best or {}).get("probability")},
            "metrics": {"coverage": dict(fam_status), "genes_total": len(g_rows), "rare_genes": rare,
                        "enrichment_facts": len(enr), "candidates_scored": len(pairs),
                        "total_seconds": span(started, finished),
                        "cache": (result.get("extract") or {}).get(iid, {}).get("cached")
                        if isinstance(result.get("extract"), dict) else None},
            "profile": {"families": FAMILY_ORDER, "new_item": dict(new_profile), "campaign_core": dict(camp_profile),
                        "campaign_id": cid},
            "graph": {"node": iid, "edges": [{"other": p["other"], "bits": p["bits"], "hypothesis": p["hypothesis"],
                                               "family": p["dominant_family"]} for p in pairs]},
            "timeline": tl,
        })
    return out


# visual aggregates live in their own module (mounted here so api.py keeps one include_router)
from hydra import api_visual as _api_visual  # noqa: E402

router.include_router(_api_visual.router)
# review queue, "niepewne" marks and similar cases (ADOPT #7, #14)
from hydra import api_review as _api_review  # noqa: E402

router.include_router(_api_review.router)
# pair geometry overlay, ORB/RootSIFT + MAGSAC++ (ADV-VIS, display only)
from hydra import api_geom as _api_geom  # noqa: E402

router.include_router(_api_geom.router)
