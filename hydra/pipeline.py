"""End-to-end run: import → genes → rarity → candidates → pair scores → campaigns/groups → (assignments).

One `run_id` per run. Every write goes through the single `hydra.db.Writer`. Gene extraction is cached per
incident by a fingerprint of (artifact ids, extractor tags): re-running an unchanged corpus skips the
extractors. Anything persisted for display is passed through `hydra.redact` first.

Storage of contract objects (migration v2): genes/gene_vectors, pairs (PairScore JSON), campaigns
(Campaign JSON) + campaign_members, assignments (Assignment JSON), runs (params + metrics). Campaign groups
are stored in `campaign_groups` (migration v3); `runs.metrics.campaign_groups` keeps a copy as a fallback.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import closing
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from hydra import events
from hydra.adapters.nask_folder import (
    ZipRejected,
    discover_incidents,
    import_incident,
    import_single,
    load_incident,
    loose_root_files,
    resolve,
    zip_skipped,
)
from hydra.assign import assign as assign_open_set
from hydra.config import Settings
from hydra.contracts import (
    Assignment,
    AssignmentDecision,
    Campaign,
    CampaignGroup,
    EnrichmentRecord,
    Gene,
    GeneStatus,
    Incident,
    PairScore,
)
from hydra.db import Writer, connect
from hydra.entropy import BackgroundPrior, Rarity, build_rarity
from hydra.genome import base
from hydra.genome.vid_seq import annotate_footage
from hydra.jobs import create_job
from hydra.nexus import Clustering, cluster
from hydra.progress import Progress
from hydra.redact import redact_obj, redact_text
from hydra.retrieval import candidates as retrieve
from hydra.storage import ContentStore
from hydra.synapse import (
    FusionModel,
    IncidentGenome,
    PairEval,
    Thresholds,
    build_genome,
    is_linked,
    robustness,
    score_pair,
)

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    run_id: str
    incidents: list[str]
    genomes: dict[str, IncidentGenome]
    rarity: Rarity
    pairs: dict[tuple[str, str], PairEval]
    clustering: Clustering
    timings: dict[str, float]
    metrics: dict[str, Any] = field(default_factory=dict)
    job_id: str | None = None


def new_run_id(kind: str) -> str:
    return f"{kind}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------------------- extraction


def enrich_client(settings: Settings) -> Any | None:
    """hydra.enrich client when the module exists (GENES-INF stream); replay-only unless HYDRA_ENRICH=1."""
    try:
        import hydra.enrich as enrich  # type: ignore[import-not-found]
    except ImportError:
        return None
    factory = getattr(enrich, "get_enricher", None)
    if callable(factory):
        try:
            return factory(None, settings)
        except Exception:  # noqa: BLE001 — enrichment is optional; genes become unavailable
            log.warning("enrich client failed to start", exc_info=True)
    return None


# ------------------------------------------------------------------ gene cache (audit A06 / A07)
#
# Per-extractor cache. Each (incident, extractor) result is keyed by a digest of: the artifact hashes, paths and
# roles, the extractor tag, ExtractContext.options, the model revisions of models.lock.json and (inf_*) the
# enrichment source state. Results live in `<work_dir>/incidents/<id>/extract_cache/<name>.json` with a small
# `<name>.state.json` beside it (digest, retry flag, attempts, time). A result with a `failed` gene, or an
# `unavailable` gene whose note is a runtime error, is retried on the next runs (at most EXTRACT_RETRIES
# attempts, waiting RETRY_BACKOFF_S between them); successful extractors stay cached, so one flaky extractor
# never forces the expensive ones to re-run. `incidents.meta.genome_fp` is the incident digest (fast path, no file
# reads); it carries a "~retry" suffix while a retry is pending.

EXTRACT_CACHE_VERSION = 2
ENRICH_CACHE_POLICY = 1          # bump when the replay/enrichment cache semantics change
RETRY_BACKOFF_S = (0.0, 60.0)    # wait after attempt 1, 2, ... before the next attempt
_RUNTIME_NOTE = re.compile(r"^[A-Z]\w*(Error|Exception|Unavailable|Timeout|Failure)\b")


def _retry_limit() -> int:
    try:
        return max(1, int(os.environ.get("HYDRA_EXTRACT_RETRIES", "3")))
    except ValueError:
        return 3


@lru_cache(maxsize=1)
def model_revisions() -> dict[str, str]:
    """{model key: revision} from models.lock.json (the weights a run can use); {} when the lock is missing."""
    lock = Path(__file__).resolve().parents[1] / "models.lock.json"
    try:
        entries = json.loads(lock.read_text("utf-8"))["entries"]
    except Exception:  # noqa: BLE001
        return {}
    return {k: str(v.get("revision") or v.get("sha256") or "") for k, v in sorted(entries.items())}


def enrich_state(settings: Settings) -> dict[str, Any]:
    try:
        from hydra.enrich.client import live_enabled

        live = live_enabled(settings)
    except ImportError:
        live = False
    try:  # snapshot commit + loaded lists + mapping digest: a new warninglist snapshot re-tiers INF genes
        from hydra.enrich.warninglists import fingerprint

        wl = fingerprint()
    except Exception:  # noqa: BLE001 — no warninglists: hand-made lists only
        wl = "none"
    return {"policy": ENRICH_CACHE_POLICY, "live": bool(live), "cache": str(settings.enrich_cache), "warninglists": wl}


def _options_digest(options: dict | None) -> str:
    return hashlib.sha256(json.dumps(options or {}, sort_keys=True, default=repr).encode()).hexdigest()[:16]


def extract_versions(settings: Settings, names: list[str], options: dict | None) -> dict[str, Any]:
    """What the gene cache is keyed on; recorded in the run manifest (`runs.params.extract_versions`)."""
    return {"cache_version": EXTRACT_CACHE_VERSION, "extractors": {n: base.REGISTRY[n].tag for n in names},
            "options_sha": _options_digest(options), "models": model_revisions(), "enrich": enrich_state(settings),
            "references": _reference_fp(settings)}


REFERENCE_FP_EMPTY = hashlib.sha256().hexdigest()[:16]


def _reference_fp(settings: Settings) -> str:
    """Face gallery / logo templates in use: a rebuilt reference base re-extracts ident + logo only."""
    try:
        from hydra.reference_build import fingerprint

        return fingerprint(settings)
    except Exception:  # noqa: BLE001
        return REFERENCE_FP_EMPTY


def _extractor_fp(inc: Incident, name: str, versions: dict[str, Any]) -> str:
    h = hashlib.sha256(f"v{versions['cache_version']}|{versions['extractors'][name]}|".encode())
    for a in sorted(f"{x.artifact_id}|{x.path_in_incident}|{x.role}" for x in inc.artifacts):
        h.update(a.encode())
    h.update(versions["options_sha"].encode())
    h.update(json.dumps(versions["models"], sort_keys=True).encode())
    if name.startswith("inf_"):
        h.update(json.dumps(versions["enrich"], sort_keys=True).encode())
    if name in ("ident", "logo") and versions.get("references") not in (None, REFERENCE_FP_EMPTY):
        h.update(str(versions["references"]).encode())  # cache key only; unchanged when no base is installed
    return h.hexdigest()[:24]


def _incident_fp(fps: dict[str, str], names: list[str]) -> str:
    return hashlib.sha256("|".join(f"{n}={fps[n]}" for n in names).encode()).hexdigest()[:16]


def _cache_dir(settings: Settings, iid: str) -> Path:
    return settings.work_dir / "incidents" / iid / "extract_cache"


def _needs_retry(res: base.ExtractResult) -> bool:
    for g in res.genes:
        if g.status == GeneStatus.FAILED:
            return True
        if g.status == GeneStatus.UNAVAILABLE and _RUNTIME_NOTE.match(g.evidence.note or ""):
            return True
    return False


def _read_state(d: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads((d / f"{name}.state.json").read_text("utf-8"))
    except Exception:  # noqa: BLE001 — missing / corrupt = miss
        return None


def _reusable(state: dict[str, Any] | None, fp: str, now: float) -> bool:
    if not state or state.get("fp") != fp:
        return False
    if not state.get("retry"):
        return True
    attempts = int(state.get("attempts", 1))
    if attempts >= _retry_limit():
        return True  # retry budget spent: keep the failed genes (failure stays visible as failed genes)
    wait = RETRY_BACKOFF_S[min(attempts - 1, len(RETRY_BACKOFF_S) - 1)]
    return now < float(state.get("at", 0.0)) + wait


def _dump_result(res: base.ExtractResult) -> str:
    return json.dumps({
        "genes": [_clean_gene(g).model_dump(mode="json") for g in res.genes],
        "vectors": [{"key": v.key, "model": v.model, "shape": list(v.data.shape),
                     "data": base64.b64encode(np.ascontiguousarray(v.data, dtype="<f4").tobytes()).decode()}
                    for v in res.vectors]}, ensure_ascii=False)


def _load_result(d: Path, name: str) -> base.ExtractResult | None:
    try:
        raw = json.loads((d / f"{name}.json").read_text("utf-8"))
        return base.ExtractResult(
            genes=[Gene.model_validate(g) for g in raw["genes"]],
            vectors=[base.Vector(key=v["key"], model=v["model"],
                                 data=np.frombuffer(base64.b64decode(v["data"]), dtype="<f4")
                                 .astype(np.float32).reshape(v["shape"])) for v in raw["vectors"]])
    except Exception:  # noqa: BLE001 — unreadable cache entry = re-run the extractor
        return None


def _write_cache(d: Path, name: str, fp: str, res: base.ExtractResult, prev: dict[str, Any] | None,
                 now: float, seconds: float | None = None, cas: tuple[Path, str, str] | None = None
                 ) -> dict[str, Any]:
    """Incident cache entry; `cas` = (CAS dir, key, incident id): a final result is also stored by content."""
    retry = _needs_retry(res)
    if cas is not None and not retry:
        _cas_store(cas[0], cas[1], cas[2], res, seconds)
    attempts = int(prev.get("attempts", 1)) + 1 if (prev and prev.get("fp") == fp and prev.get("retry")) else 1
    state: dict[str, Any] = {"fp": fp, "retry": retry, "attempts": attempts, "at": now}
    if seconds is not None:  # original compute time, reported by later runs that reuse this entry
        state["seconds"] = round(float(seconds), 4)
    try:
        d.mkdir(parents=True, exist_ok=True)
        for suffix, text in ((".json", _dump_result(res)), (".state.json", json.dumps(state))):
            tmp = d / f"{name}{suffix}.tmp"
            tmp.write_text(text, "utf-8")
            tmp.replace(d / f"{name}{suffix}")
    except OSError as exc:
        log.warning("gene cache write failed for %s: %s", name, exc)
    return state


# CAS-level gene cache (SCALE 2026-11-21): extractor results addressed by content, shared by every incident and
# every state dir on the host (`$HYDRA_CACHE_DIR/genes-cas/`). The incident cache above answers "did this incident
# change"; this one answers "was this exact input already extracted anywhere" (re-imports, the same folder under a
# new id, a fresh state on a known corpus). The key is the extractor fingerprint (version, artifact sha256 + path +
# role, options, model revisions, enrichment state), the incident fields extractors read (`meta` url keys,
# `observed_at`), the numerics (torch threads, device, dtype: bf16 vectors depend on them, SPEC_ERRATA #14), the
# model weights present and a digest of the extraction code, so an edited extractor never reads stale genes.
# Results are stored with the incident id replaced by a placeholder; an entry whose JSON still mentions the id
# elsewhere is not stored.
# Never cached: dependent (NAR) extractors (their input is other extractors' output, failures included), `inf_*`
# with live enrichment, extractors defined outside `hydra.` (tests), results due for a retry. Off: HYDRA_GENE_CAS=0.
GENE_CAS_VERSION = 1
_CAS_IID = "<incident>"


def gene_cas_dir(settings: Settings) -> Path | None:
    if os.environ.get("HYDRA_GENE_CAS", "1") == "0" or settings.cache_dir is None:
        return None
    return settings.cache_dir / "genes-cas" / f"v{GENE_CAS_VERSION}"


@lru_cache(maxsize=1)
def extraction_code_digest() -> str:
    """sha256 over the extraction code and its data files (genome lexicons/assets)."""
    root = Path(__file__).resolve().parent
    files = [f for f in (root / "genome").rglob("*") if f.is_file() and "__pycache__" not in f.parts]
    for sub in ("ml", "enrich", "adapters"):
        files += [f for f in (root / sub).rglob("*.py") if "__pycache__" not in f.parts]
    files += [root / n for n in ("media.py", "contracts.py", "redact.py", "config.py", "storage.py", "pipeline.py",
                                 "parallel.py")]
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.relative_to(root).as_posix().encode() + b"|")
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def _numerics() -> dict[str, str]:
    from hydra.parallel import MAX_TORCH_THREADS

    threads = os.environ.get("HYDRA_TORCH_THREADS") or str(max(1, min(MAX_TORCH_THREADS, os.cpu_count() or 1)))
    return {"torch_threads": threads, "device": os.environ.get("HYDRA_DEVICE", "auto"),
            "dtype": os.environ.get("HYDRA_TORCH_DTYPE", ""), "platform": f"{sys.platform}-{platform.machine()}"}


def cas_cacheable(name: str, versions: dict[str, Any]) -> bool:
    ex = base.REGISTRY.get(name)
    return (ex is not None and ex.fn.__module__.startswith("hydra.") and not is_dependent(name)
            and not (name.startswith("inf_") and versions["enrich"].get("live")))


@lru_cache(maxsize=8)
def _models_present(models_dir: str) -> tuple[str, ...]:
    """Model weights available offline (missing weights give `unavailable` genes, so they are part of the key)."""
    d = Path(models_dir)
    try:
        return tuple(sorted(x.name for x in d.iterdir() if x.is_dir() and any(x.iterdir())))
    except OSError:
        return ()


def cas_key(inc: Incident, fp: str, settings: Settings) -> str:
    payload = {"fp": fp, "code": extraction_code_digest(), "numerics": _numerics(),
               "models_present": _models_present(str(settings.models_dir)),
               "meta": {k: inc.meta.get(k) for k in ("url", "landing_url", "entry_url")},
               "observed_at": str(inc.observed_at) if inc.observed_at is not None else None}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _cas_path(cas: Path, key: str) -> Path:
    return cas / key[:2] / f"{key}.json"


def _rebind(raw: dict[str, Any], old: str, new: str) -> dict[str, Any]:
    pre, new_pre = f"{old}|", f"{new}|"
    for g in raw["genes"]:
        if g.get("incident_id") == old:
            g["incident_id"] = new
        if isinstance(g.get("vector_ref"), str) and g["vector_ref"].startswith(pre):
            g["vector_ref"] = new_pre + g["vector_ref"][len(pre):]
    for v in raw["vectors"]:
        if v["key"].startswith(pre):
            v["key"] = new_pre + v["key"][len(pre):]
    return raw


def _cas_store(cas: Path, key: str, iid: str, res: base.ExtractResult, seconds: float | None = None) -> None:
    try:
        raw = _rebind(json.loads(_dump_result(res)), iid, _CAS_IID)
        if seconds is not None:
            raw["seconds"] = round(float(seconds), 4)
        text = json.dumps(raw, ensure_ascii=False)
        if iid in text:  # the id leaks into values / evidence: not addressable by content
            return
        path = _cas_path(cas, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(path)
    except (OSError, ValueError) as exc:
        log.warning("gene CAS write failed: %s", exc)


def _cas_restore(cas: Path, key: str, iid: str, d: Path, name: str, fp: str, now: float) -> dict[str, Any] | None:
    """Copy a CAS entry into the incident cache (rebound to `iid`); returns its cache state, or None on a miss."""
    try:
        raw = json.loads(_cas_path(cas, key).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        secs = raw.pop("seconds", None)
        text = json.dumps(_rebind(raw, _CAS_IID, iid), ensure_ascii=False)
        state = {"fp": fp, "retry": False, "attempts": 1, "at": now, "cas": key[:16]}
        if secs is not None:
            state["seconds"] = secs
        d.mkdir(parents=True, exist_ok=True)
        for suffix, body in ((".json", text), (".state.json", json.dumps(state))):
            tmp = d / f"{name}{suffix}.tmp"
            tmp.write_text(body, "utf-8")
            tmp.replace(d / f"{name}{suffix}")
        return state
    except OSError as exc:
        log.warning("gene CAS restore failed for %s: %s", name, exc)
        return None


def _clean_gene(g: Gene) -> Gene:
    g = g.model_copy()
    if g.value is not None:
        g.value = redact_text(g.value)
    if g.evidence.note:
        g.evidence = g.evidence.model_copy(update={"note": redact_text(g.evidence.note)})
    return g


def enrichment_records(resolved: base.ResolvedIncident, enrich: Any | None) -> list[EnrichmentRecord]:
    """`hydra.enrich.records_for` (replay-only unless HYDRA_ENRICH=1); unavailable module or failure → []."""
    try:
        from hydra.enrich import records_for  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        return list(records_for(resolved, enrich))
    except Exception:  # noqa: BLE001 — enrichment is evidence, never a reason to fail the run
        log.warning("enrichment records failed for %s", resolved.incident.id, exc_info=True)
        return []


def _vector_key_range(iid: str) -> tuple[str, str]:
    """Vector keys of one incident (`<iid>|...`) as a primary-key range. `key LIKE 'iid|%'` cannot use the index
    (LIKE is case-insensitive, `_` in ids is a wildcard), so every incident scanned the whole table: O(N^2)."""
    return f"{iid}|", f"{iid}}}"  # '}' sorts right after '|'


def gene_identity(family: Any, kind: str, value: str | None, vector_ref: str | None, status: Any, provenance: Any,
                  extractor: str) -> str:
    """What makes two stored genes the same piece of knowledge across re-extractions (CR-ASTRA-RES-4). The
    extractor version is left out: the same value read again by a newer extractor was already known."""
    body = value if value is not None else f"vec:{vector_ref}"
    return f"{family}|{kind}|{body}|{status}|{provenance}|{str(extractor).split('@', 1)[0]}"


def _evidence_versions(conn: sqlite3.Connection, iid: str, genes: list[Gene], fp: str, now: str,
                       records: list[EnrichmentRecord] | None) -> dict[str, Any]:
    """Evidence availability without a migration (CR-ASTRA-RES-4). `genes.created_at` is the time HYDRA first held
    that gene: an unchanged gene keeps it across a re-extraction, a new one gets `now` (extraction / enrichment
    completion). Every distinct gene set is an evidence version in `incidents.meta.evidence_versions`; value genes
    dropped by a correction go to `incidents.meta.evidence_retired` with their availability window, so the previous
    version stays reproducible. An unknown earlier time is never kept (it would back-date): such a gene gets `now`."""
    row = conn.execute("SELECT created_at, observed_at, meta FROM incidents WHERE id = ?", (iid,)).fetchone()
    db_meta = json.loads(row["meta"] or "{}") if row is not None else {}
    versions = list(db_meta.get("evidence_versions") or [])
    retired = list(db_meta.get("evidence_retired") or [])
    old = conn.execute("SELECT family, kind, value, vector_ref, status, provenance, evidence, extractor,"
                       " dependency_group, created_at FROM genes WHERE incident_id = ?", (iid,)).fetchall()
    first_at: dict[str, str] = {}
    old_keys: set[str] = set()
    for r in old:
        k = gene_identity(r["family"], r["kind"], r["value"], r["vector_ref"], r["status"], r["provenance"],
                          r["extractor"])
        old_keys.add(k)
        t = r["created_at"] or ""
        pt, pn = _parse_time(t), _parse_time(now)
        if pt is not None and pn is not None and pt <= pn and (k not in first_at or pt < _parse_time(first_at[k])):
            first_at[k] = t
    new_keys = {gene_identity(g.family, g.kind, g.value, g.vector_ref, g.status, g.provenance, g.extractor)
                for g in genes}
    prev = versions[-1]["id"] if versions else None
    gone = 0
    for r in old:
        k = gene_identity(r["family"], r["kind"], r["value"], r["vector_ref"], r["status"], r["provenance"],
                          r["extractor"])
        if k in new_keys or r["value"] is None:
            continue
        gone += 1
        retired.append({"family": r["family"], "kind": r["kind"], "value": r["value"], "status": r["status"],
                        "provenance": r["provenance"], "extractor": r["extractor"],
                        "dependency_group": r["dependency_group"], "evidence": json.loads(r["evidence"] or "{}"),
                        "available_from": first_at.get(k), "retired_at": now, "version": prev})
    vid = "ev-" + hashlib.sha256(events.canonical_json([iid, sorted(new_keys)]).encode()).hexdigest()[:16]
    if not versions or versions[-1]["id"] != vid:
        versions.append({"version": len(versions) + 1, "id": vid, "completed_at": now,
                         "acquired_at": row["created_at"] if row is not None else None,
                         "source_time": row["observed_at"] if row is not None else None, "genome_fp": fp,
                         "genes": len(new_keys), "added": len(new_keys - old_keys), "retired": gone,
                         "enrichment_fetched_at": max((r.fetched_at for r in records or [] if r.fetched_at),
                                                      default=None)})
    return {"evidence_versions": versions, "evidence_retired": retired,
            "_first_at": {k: v for k, v in first_at.items() if k in new_keys}}


def _parse_time(s: str) -> Any:
    from datetime import UTC, datetime

    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except (TypeError, ValueError):
        return None
    return d.replace(tzinfo=UTC) if d is not None and d.tzinfo is None else d


def _persist_genes(conn: sqlite3.Connection, iid: str, res: base.ExtractResult, fp: str, meta: dict,
                   records: list[EnrichmentRecord] | None = None) -> None:
    """Genes, vectors and the enrichment records behind them, in one writer transaction."""
    now = events.utcnow()
    conn.executemany(
        "INSERT OR REPLACE INTO enrichment (key, source, query, fetched_at, status, response_sha256, cache_path,"
        " summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(r.key, r.source, redact_text(r.query), r.fetched_at, r.status, r.response_sha256,
          redact_text(r.cache_path), events.canonical_json(redact_obj(r.summary))) for r in records or []],
    )
    clean = [_clean_gene(x) for x in res.genes]
    meta = {**meta, **_evidence_versions(conn, iid, clean, fp, now, records)}
    first_at = meta.pop("_first_at")
    conn.execute("DELETE FROM genes WHERE incident_id = ?", (iid,))
    conn.execute("DELETE FROM gene_vectors WHERE key >= ? AND key < ?", _vector_key_range(iid))
    conn.executemany(
        "INSERT INTO genes (incident_id, family, kind, value, vector_ref, quality, status, provenance, evidence,"
        " extractor, dependency_group, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(iid, g.family, g.kind, g.value, g.vector_ref, g.quality, g.status, g.provenance,
          events.canonical_json(g.evidence.model_dump(exclude_none=True)), g.extractor, g.dependency_group,
          first_at.get(gene_identity(g.family, g.kind, g.value, g.vector_ref, g.status, g.provenance, g.extractor),
                       now))
         for g in clean],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO gene_vectors (key, model, dim, data) VALUES (?, ?, ?, ?)",
        [(v.key, v.model, int(v.data.shape[-1]), np.ascontiguousarray(v.data, dtype="<f4").tobytes())
         for v in res.vectors],
    )
    meta = {**meta, "genome_fp": fp, "genes": len(res.genes), "vectors": len(res.vectors)}
    conn.execute("UPDATE incidents SET meta = ? WHERE id = ?", (events.canonical_json(meta), iid))
    events.append(conn, "GENES_EXTRACTED", {"incident_id": iid, "genes": len(res.genes), "vectors": len(res.vectors),
                                            "fingerprint": fp})


def _extract_one(inc: Incident, store: ContentStore, settings: Settings, names: list[str], enrich: Any | None,
                 ctx_models: dict[str, Any], options: dict | None,
                 on_extractor: Callable[[str, base.ExtractResult, float], None] | None = None
                 ) -> tuple[base.ExtractResult, list[EnrichmentRecord]]:
    """Genes, vectors and enrichment records of one incident (compute only, no DB writes). With `on_extractor`
    the extractors run one by one (same order and results as run_all(only=names)) and each is timed."""
    work = settings.work_dir / "incidents" / inc.id
    work.mkdir(parents=True, exist_ok=True)
    ctx = base.ExtractContext(settings=settings, work_dir=work, enrich=enrich, models=ctx_models,
                              options=dict(options or {}))
    resolved = resolve(inc, store)
    if on_extractor is None:
        res = base.run_all(resolved, ctx, only=names)
    else:
        res = base.ExtractResult()
        for n in names:
            t0 = time.perf_counter()
            part = base.run_all(resolved, ctx, only=[n])
            on_extractor(n, part, time.perf_counter() - t0)
            res.genes.extend(part.genes)
            res.vectors.extend(part.vectors)
    t0 = time.perf_counter()
    recs = enrichment_records(resolved, enrich) if any(n.startswith("inf_") for n in names) else []
    if on_extractor is not None and recs:
        on_extractor("enrich", base.ExtractResult(), time.perf_counter() - t0)
    return res, recs


def _extract_job(inc: Incident, cas_dir: Path, settings: Settings, names: list[str], options: dict | None,
                 cache: tuple[dict[str, str], dict[str, Any], float, dict[str, Any]] | None, state: dict[str, Any]
                 ) -> tuple[dict[str, base.ExtractResult], list[EnrichmentRecord], float, dict[str, float],
                            dict[str, dict[str, Any]]]:
    """`hydra.parallel` worker entry for one (incident, extractor group), or for the dependent (NAR) extractors of
    one incident (`options["incident_genes"]` set by the parent). Models and the enrich client live in `state` for
    the worker's lifetime (as `ctx_models` does for the whole sequential loop). Results are kept per extractor so the
    caller can restore the exact `run_all` order of the sequential path. `cache` = (fingerprints, previous cache
    states, now, CAS targets): the worker writes the gene cache files of its extractors itself and returns their new
    states."""
    t0 = time.perf_counter()
    if "models" not in state:
        base.load_family_modules()
        import hydra.genome.artifact  # noqa: F401
        state["models"] = {}
        state["enrich"] = enrich_client(settings)
    # Extractors registered in the parent outside the family modules (e.g. inf_feed from hydra.adapters.public_feeds)
    # are unknown to a fresh spawn worker: import their modules, named by the parent in options.
    if any(n not in base.REGISTRY for n in names):
        for mod in (options or {}).get("_extractor_modules", ()):
            importlib.import_module(mod)
    enrich = state["enrich"]
    work = settings.work_dir / "incidents" / inc.id
    work.mkdir(parents=True, exist_ok=True)
    ctx = base.ExtractContext(settings=settings, work_dir=work, enrich=enrich, models=state["models"],
                              options=dict(options or {}))
    resolved = resolve(inc, ContentStore(cas_dir))
    per: dict[str, base.ExtractResult] = {}
    timings: dict[str, float] = {}
    for n in names:  # same loop as run_all(only=names); each extractor timed for the progress view
        t1 = time.perf_counter()
        per[n] = base.run_all(resolved, ctx, only=[n])
        timings[n] = time.perf_counter() - t1
    t1 = time.perf_counter()
    recs = enrichment_records(resolved, enrich) if any(n.startswith("inf_") for n in names) else []
    if recs:
        timings["enrich"] = time.perf_counter() - t1
    state["last_timings"] = timings
    states: dict[str, dict[str, Any]] = {}
    if cache is not None:
        fps, prev, now, cas = cache
        d = _cache_dir(settings, inc.id)
        states = {n: _write_cache(d, n, fps[n], per[n], prev.get(n), now, seconds=timings.get(n), cas=cas.get(n))
                  for n in names}
    return per, recs, time.perf_counter() - t0, timings, states


def _parallel_ok(names: list[str], options: dict | None) -> bool:
    """Workers re-import the extractors by module: test-only extractors and unpicklable options stay in-process."""
    from hydra import parallel

    return all(base.REGISTRY[n].fn.__module__.startswith("hydra.") for n in names) and parallel.picklable(options)


def extract(incidents: list[Incident], writer: Writer, store: ContentStore, settings: Settings, *,
            only: list[str] | None = None, use_cache: bool = True, options: dict | None = None,
            on_done: Callable[[Incident, base.ExtractResult | None], None] | None = None,
            on_extractor: Callable[[str, base.ExtractResult, float], None] | None = None
            ) -> dict[str, dict[str, Any]]:
    """Run the registered extractors on each incident and persist genes. Returns per-incident stats.

    Cache: per extractor (see the gene-cache notes above); only extractors whose digest changed, or whose last
    result failed and is due for a retry, run again. Incidents are computed in a process pool when more than one
    needs work (`hydra.parallel.plan`); results are persisted by this process only, in input order, one writer
    transaction per incident, genes in registration order exactly as `run_all(only=names)`.
    """
    from hydra import parallel

    base.load_family_modules()
    import hydra.genome.artifact  # noqa: F401 — CORE extractor (exact duplicates), registers itself

    names = list(only) if only is not None else list(base.REGISTRY)
    versions = extract_versions(settings, names, options)
    now = time.time()
    stats: dict[str, dict[str, Any]] = {}
    # (incident, incident fp, per-extractor fps, extractors to run, previous states)
    todo: list[tuple[Incident, str, dict[str, str], list[str], dict[str, Any]]] = []
    dependents = [n for n in names if is_dependent(n)]
    cas = gene_cas_dir(settings)
    cas_ok = {n: cas_cacheable(n, versions) for n in names}
    cas_hits: dict[str, int] = {}

    def cas_of(inc: Incident, n: str, fps: dict[str, str]) -> tuple[Path, str, str] | None:
        return (cas, cas_key(inc, fps[n], settings), inc.id) if cas is not None and cas_ok[n] else None

    for inc in incidents:
        fps = {n: _extractor_fp(inc, n, versions) for n in names}
        # a dependent (NAR) extractor reads the incident's other genes: its digest includes theirs (A07)
        base_digest = "|".join(f"{n}={fps[n]}" for n in names if n not in dependents)
        for n in dependents:
            fps[n] = hashlib.sha256(f"{fps[n]}|deps:{base_digest}".encode()).hexdigest()[:24]
        ifp = _incident_fp(fps, names)
        if use_cache and inc.meta.get("genome_fp") == ifp:  # fast path: nothing pending, no file reads
            run: list[str] = []
            states: dict[str, Any] = {}
        else:
            d = _cache_dir(settings, inc.id)
            states = {n: _read_state(d, n) for n in names} if use_cache else {}
            run = [n for n in names if not (use_cache and _reusable(states.get(n), fps[n], now))]
            if cas is not None and use_cache and run:
                for n in [n for n in run if cas_ok[n]]:
                    st = _cas_restore(cas, cas_key(inc, fps[n], settings), inc.id, d, n, fps[n], now)
                    if st is not None:
                        states[n] = st
                        run.remove(n)
                        cas_hits[inc.id] = cas_hits.get(inc.id, 0) + 1
            if not run and inc.meta.get("genome_fp") != f"{ifp}~retry":
                # every extractor is reusable but the incident row was not written by these results (meta reset,
                # interrupted run): rebuild it from the cache files without running anything
                stats[inc.id] = {}
                todo.append((inc, ifp, fps, run, states))
                continue
        if not run:  # nothing to do (retries, if any, not due yet): the DB already holds these genes
            stats[inc.id] = {"cached": True, "genes": inc.meta.get("genes", 0), "seconds": 0.0,
                             "extractors_run": 0, "extractors_cached": len(names),
                             "extractor_seconds": {},
                             "extractor_original_seconds": _original_seconds(inc, names, states)}
            if on_done:
                on_done(inc, None)
            continue
        stats[inc.id] = {}  # placeholder keeps the input order of the stats dict
        todo.append((inc, ifp, fps, run, states))

    def persist(k: int, per: dict[str, base.ExtractResult], recs: list[EnrichmentRecord], seconds: float,
                ok: bool = True, tim: dict[str, float] | None = None,
                written: dict[str, dict[str, Any]] | None = None, wait: bool = True) -> Future | None:
        inc, ifp, fps, run, states = todo[k]
        d = _cache_dir(settings, inc.id)
        res = base.ExtractResult()
        for n in names:  # registration order, exactly as run_all(only=names)
            part = per.get(n) or _load_result(d, n)
            if part is None:  # cache file vanished since the state check: unknown, retried next run
                part = base.ExtractResult(genes=[base.failed_gene(inc, base.REGISTRY[n],
                                                                  RuntimeError("gene cache entry missing"))])
                per[n] = part
            res.genes.extend(part.genes)
            res.vectors.extend(part.vectors)
        written = written or {}
        tim = tim or {}
        new_states = {n: written.get(n) or _write_cache(d, n, fps[n], per[n], states.get(n), now, seconds=tim.get(n),
                                                         cas=cas_of(inc, n, fps))
                      for n in run if n in per}
        all_states = [new_states.get(n) or states.get(n) or {} for n in names]
        pending = any(st.get("retry") and int(st.get("attempts", 1)) < _retry_limit() for st in all_states)
        f = f"{ifp}~retry" if pending else ifp
        meta = dict(inc.meta)
        orig = _original_seconds(inc, names, {**states, **new_states})
        meta["extractor_seconds"] = orig
        fut = writer.submit(lambda conn, i=inc.id, r=res, m=meta, f=f, e=recs: _persist_genes(conn, i, r, f, m, e))
        if wait:
            fut.result(timeout=600)
        inc.meta.update({"genome_fp": f, "genes": len(res.genes), "extractor_seconds": orig})
        stats[inc.id] = {"cached": False, "genes": len(res.genes), "vectors": len(res.vectors),
                         "seconds": round(seconds, 3), "extractors_run": len(run),
                         "extractors_cached": len(names) - len(run), "extractors_from_cas": cas_hits.get(inc.id, 0),
                         "retry_pending": pending,
                         "extractor_seconds": {n: round(v, 3) for n, v in tim.items()},
                         "extractor_original_seconds": orig}
        if not ok:
            stats[inc.id]["worker_failed"] = True
        if on_done:
            on_done(inc, res)
        return None if wait else fut

    groups = _extractor_groups(names)
    n_jobs = sum(1 for _inc, _f, _fps, run, _s in todo for g in groups if set(g) & set(run))
    # one incident (assign, single upload) stays in-process: worker start-up + model loads would dominate
    plan = (parallel.plan(n_jobs, settings.workers)
            if len(todo) > 1 and n_jobs > 1 and _parallel_ok(names, options) else parallel.Plan(0, 0, 0))
    if not plan.parallel:
        # models live for the process (as in a pool worker): one assign / upload after another reloaded e5,
        # DINOv2 and the face models every time
        ctx_models: dict[str, Any] = _PROCESS_MODELS.setdefault(f"{settings.models_dir}|{settings.cache_dir}", {})
        enrich = enrich_client(settings)
        for k, (inc, _ifp, _fps, run, _states) in enumerate(todo):
            t0 = time.perf_counter()
            per: dict[str, base.ExtractResult] = {}
            tim: dict[str, float] = {}

            def keep(n: str, part: base.ExtractResult, sec: float, per: dict = per, tim: dict = tim) -> None:
                per[n] = part
                tim[n] = sec
                if on_extractor:
                    on_extractor(n, part, sec)

            recs: list[EnrichmentRecord] = []
            run_base = [n for n in run if n not in dependents]
            if run_base:
                _res, recs = _extract_one(inc, store, settings, run_base, enrich, ctx_models, options, keep)
            _run_dependents(inc, store, settings, [n for n in run if n in dependents], names, dependents, per,
                            ctx_models, enrich, options, keep)
            persist(k, per, recs, time.perf_counter() - t0, tim=tim)
        return stats

    # Parallel path. Phase 1: one job per (incident, extractor group), most expensive first. Phase 2: once all
    # phase-1 jobs of an incident are back, its dependent (NAR) extractors run as one more pool job with the same
    # `incident_genes` the sequential path builds. The parent only merges and persists, in input order; workers
    # write their gene cache files and DB writes are queued on the writer without waiting (checked at the end).
    options = {**(options or {}), "_extractor_modules": sorted(
        {base.REGISTRY[n].fn.__module__ for g in groups for n in g if n in base.REGISTRY})}
    first: list[tuple[int, list[str]]] = []
    for k, (_inc, _ifp, _fps, run, _states) in enumerate(todo):
        for g in groups:
            sub = [n for n in g if n in run and n not in dependents]  # dependents run after phase 1
            if sub:
                first.append((k, sub))
    left = [0] * len(todo)
    for k, _sub in first:
        left[k] += 1
    log.info("extracting %d incidents as %d jobs in %d processes (torch threads %d, OCR workers %d per process,"
             " torch slots %d)", len(todo), len(first), plan.workers, plan.torch_threads, plan.ocr_workers,
             plan.torch_slots)

    def job_args(k: int, sub: list[str], opts: dict) -> tuple:
        inc, _ifp, fps, _run, states = todo[k]
        return (inc, store.root, settings, sub, opts, ({n: fps[n] for n in sub}, {n: states.get(n) for n in sub},
                                                         now, {n: cas_of(inc, n, fps) for n in sub}))

    job_names: dict[tuple[int, int], list[str]] = {}

    def crashed(key: tuple[int, int], exc: BaseException):
        inc = todo[key[0]][0]
        err = RuntimeError(f"extraction worker failed: {type(exc).__name__}: {exc}")
        return ({n: base.ExtractResult(genes=[base.failed_gene(inc, base.REGISTRY[n], err)]) for n in job_names[key]},
                [], 0.0, {}, {})

    per_k: list[dict[str, base.ExtractResult]] = [{} for _ in todo]
    recs_k: list[list[EnrichmentRecord]] = [[] for _ in todo]
    sec_k = [0.0] * len(todo)
    ok_k = [True] * len(todo)
    tim_k: list[dict[str, float]] = [{} for _ in todo]
    written_k: list[dict[str, dict[str, Any]]] = [{} for _ in todo]
    ready = [False] * len(todo)
    futs: list[Future] = []
    nxt = 0
    pool = parallel.Pool(plan, _extract_job, crashed)

    def submit(k: int, j: int, sub: list[str], opts: dict) -> None:
        job_names[(k, j)] = sub
        pool.submit((k, j), job_args(k, sub, opts))

    def phase1_done(k: int) -> None:
        dep_run = [n for n in todo[k][3] if n in dependents]
        if not dep_run:
            ready[k] = True
            return
        inc = todo[k][0]
        d = _cache_dir(settings, inc.id)
        genes: list[Gene] = []
        for n in names:  # registration order, fresh results or the cache (as _run_dependents)
            if n in dependents:
                continue
            part = per_k[k].get(n) or _load_result(d, n)
            if part is not None:
                genes.extend(part.genes)
        submit(k, -1, dep_run, {**options, "incident_genes": genes})

    try:
        order = sorted(range(len(first)), key=lambda j: _job_cost(todo[first[j][0]][0], first[j][1]), reverse=True)
        for j in order:
            submit(first[j][0], j, first[j][1], options)
        for k in range(len(todo)):
            if left[k] == 0:
                phase1_done(k)
        while nxt < len(todo):
            while nxt < len(todo) and ready[nxt]:
                futs.append(persist(nxt, per_k[nxt], recs_k[nxt], sec_k[nxt], ok_k[nxt], tim=tim_k[nxt],
                                    written=written_k[nxt], wait=False))
                while len(futs) > MAX_PENDING_WRITES:  # other writers (progress, jobs) must not queue behind
                    fut = futs.pop(0)
                    if fut is not None:
                        fut.result(timeout=600)
                per_k[nxt] = {}  # free the results once queued on the writer
                nxt += 1
            if nxt >= len(todo):
                break
            (k, j), (res, rec, sec, tim, written), ok = pool.next_result()
            per_k[k].update(res)
            recs_k[k].extend(rec)
            # wall seconds of the incident: its phase-1 jobs run side by side, the dependent job after them
            sec_k[k] = sec_k[k] + sec if j == -1 else max(sec_k[k], sec)
            ok_k[k] = ok_k[k] and ok
            tim_k[k].update(tim)
            written_k[k].update(written)
            if on_extractor is not None:
                for n, t_n in tim.items():
                    on_extractor(n, res.get(n, base.ExtractResult()), t_n)
            if j == -1:
                ready[k] = True
            else:
                left[k] -= 1
                if left[k] == 0:
                    phase1_done(k)
    finally:
        pool.close()
    for f in futs:
        if f is not None:
            f.result(timeout=600)
    return stats


def _original_seconds(inc: Incident, names: list[str], states: dict[str, Any]) -> dict[str, float]:
    """Per extractor: the compute time of the result the incident's genes come from (this run's when it ran, else
    the time stored with the reused cache entry / in incidents.meta). Entries written before timings were stored
    have no time and are left out (unknown, never 0)."""
    known = dict(inc.meta.get("extractor_seconds") or {})
    out: dict[str, float] = {}
    for n in names:
        st = states.get(n) or {}
        v = st.get("seconds", known.get(n))
        if v is not None:
            out[n] = round(float(v), 4)
    return out


def _backfill_original_seconds(settings: Settings, ex_stats: dict[str, dict[str, Any]], names: list[str],
                               max_runs: int = 40) -> None:
    """Gene-cache entries written before compute times were stored: take the time from the newest earlier run
    that actually ran that extractor on that incident (runs.metrics.extract.<id>.extractor_seconds)."""
    missing = {iid: {n for n in names if n not in (st.get("extractor_original_seconds") or {})}
               for iid, st in ex_stats.items()}
    missing = {k: v for k, v in missing.items() if v}
    if not missing:
        return
    with closing(connect(settings.db_path, readonly=True)) as conn:
        rows = conn.execute("SELECT metrics FROM runs WHERE kind='analyse' ORDER BY created_at DESC LIMIT ?",
                            (max_runs,)).fetchall()
    for r in rows:
        try:
            ext = (json.loads(r["metrics"]) or {}).get("extract") or {}
        except (TypeError, ValueError):
            continue
        for iid in list(missing):
            got = (ext.get(iid) or {}).get("extractor_seconds") or {}
            for n in list(missing[iid]):
                if n in got:
                    ex_stats[iid].setdefault("extractor_original_seconds", {})[n] = float(got[n])
                    missing[iid].discard(n)
            if not missing[iid]:
                del missing[iid]
        if not missing:
            break


# ctx.models of the sequential path per (models dir, cache dir): models, OCR pool, static galleries
_PROCESS_MODELS: dict[str, dict[str, Any]] = {}
MAX_PENDING_WRITES = 16  # gene persists queued on the writer ahead of the parent (more starve progress writes)


# Extractors that share an in-process cache must run in the same job: vis/ident/logo share the visual units
# (vis_media.collect_units), the TXT family + nar + meta share txt_sources.collect (OCR, ASR) and aud_* the
# Whisper model; inf_* share the enrich client and the enrichment records are computed after them. Everything
# else joins the last group.
_GROUPS: tuple[tuple[str, ...], ...] = (
    ("vis", "ident", "logo"),
    ("txt_lines", "txt_phrases", "txt_entities", "txt_embed", "nar", "meta", "aud_asr", "aud_fp"),
)


def is_dependent(name: str) -> bool:
    """NAR extractors (e.g. `nar_funnel`) read the incident's other genes, so they run after INF/VIS/TXT/ID/META."""
    ex = base.REGISTRY.get(name)
    return ex is not None and str(ex.family) == "NAR"


def _run_dependents(inc: Incident, store: ContentStore, settings: Settings, dep_run: list[str], names: list[str],
                    dependents: list[str], per: dict[str, base.ExtractResult], ctx_models: dict[str, Any],
                    enrich: Any, options: dict | None, keep: Callable) -> None:
    """Run dependent extractors with `ctx.options["incident_genes"]` = the genes of every non-dependent extractor
    of this incident (fresh results or the cache), in registration order. Same input in the sequential and the
    parallel path, so the results are identical."""
    if not dep_run:
        return
    d = _cache_dir(settings, inc.id)
    genes: list[Gene] = []
    for n in names:
        if n in dependents:
            continue
        part = per.get(n) or _load_result(d, n)
        if part is not None:
            genes.extend(part.genes)
    opts = {**(options or {}), "incident_genes": genes}
    _extract_one(inc, store, settings, dep_run, enrich, ctx_models, opts, keep)


def _extractor_groups(names: list[str]) -> list[list[str]]:
    """`names` split into independent groups, each keeping registration order; empty groups dropped."""
    member = {n: i for i, g in enumerate(_GROUPS) for n in g}
    out: list[list[str]] = [[] for _ in range(len(_GROUPS) + 1)]
    for n in names:
        out[member.get(n, len(_GROUPS))].append(n)
    return [g for g in out if g]


def _job_cost(inc: Incident, group: list[str]) -> float:
    """Submission priority: visual work on big incidents (videos) first, then OCR/text, then the rest."""
    size = sum(int(a.size or 0) for a in inc.artifacts)
    weight = 3.0 if "vis" in group else 2.0 if "txt_lines" in group else 1.0
    return weight * (1.0 + size)


# --------------------------------------------------------------------------------------- corpus


def _row_gene(r: sqlite3.Row) -> Gene:
    return Gene(incident_id=r["incident_id"], family=r["family"], kind=r["kind"], value=r["value"],
                vector_ref=r["vector_ref"], quality=r["quality"], status=r["status"], provenance=r["provenance"],
                evidence=json.loads(r["evidence"] or "{}"), extractor=r["extractor"],
                dependency_group=r["dependency_group"])


def load_genomes(conn: sqlite3.Connection, incident_ids: list[str], digest: Any | None = None
                 ) -> dict[str, IncidentGenome]:
    """`digest` (a hashlib object, optional) is fed every row the genomes are built from (incremental analyse)."""
    out: dict[str, IncidentGenome] = {}
    for iid in incident_ids:
        inc = conn.execute("SELECT title, observed_at, label FROM incidents WHERE id = ?", (iid,)).fetchone()
        if inc is None:
            continue
        rows = conn.execute("SELECT * FROM genes WHERE incident_id = ? ORDER BY id", (iid,)).fetchall()
        vrows = conn.execute("SELECT key, model, data FROM gene_vectors WHERE key >= ? AND key < ? ORDER BY key",
                             _vector_key_range(iid)).fetchall()
        if digest is not None:
            digest.update(repr((iid, tuple(inc))).encode())
            for r in rows:
                digest.update(repr(tuple(r[k] for k in r.keys() if k not in ("id", "created_at"))).encode())
            for r in vrows:
                digest.update(repr((r["key"], r["model"])).encode())
                digest.update(r["data"])
        genes = [_row_gene(r) for r in rows]
        vecs = {r["key"]: (r["model"], np.frombuffer(r["data"], dtype="<f4").astype(np.float32)) for r in vrows}
        out[iid] = build_genome(iid, genes, vecs, label=inc["label"], title=inc["title"],
                                observed_at=inc["observed_at"])
    annotate_footage(out)  # shared-footage frequency per video frame (stock / news clips → boilerplate)
    return out


def rarity_for(genomes: dict[str, IncidentGenome], unit_of: dict[str, str] | None = None,
               prior: BackgroundPrior | None = None) -> Rarity:
    vecs: dict[tuple[str, str], dict[str, np.ndarray]] = {}
    for iid, g in genomes.items():
        for key, (mat, _genes) in g.vectors.items():
            vecs.setdefault(key, {})[iid] = mat
    r = build_rarity({iid: g.genes for iid, g in genomes.items()}, vecs, unit_of=unit_of)
    r.prior = with_shipped_prior(prior)
    r.member_ids = frozenset(genomes)  # type: ignore[attr-defined] — incidents counted (incl. gene-less ones)
    return r


def with_shipped_prior(prior: BackgroundPrior | None) -> Any:
    """The shipped hashed background prior (`hydra.priors`, switch HYDRA_BACKGROUND_PRIOR) combined with an
    explicit `--prior` corpus, when both exist."""
    from hydra.entropy import CompositePrior
    from hydra.priors import default_prior

    shipped = default_prior()
    if shipped is None:
        return prior
    return shipped if prior is None else CompositePrior([shipped, prior])


def extractor_summary(ex_stats: dict[str, dict[str, Any]], ex_totals: dict[str, list[float]],
                      genomes: dict[str, IncidentGenome], names: list[str]) -> dict[str, dict[str, Any]]:
    """Per extractor: seconds spent in this run, original compute seconds of the results used (cache entries keep
    their time), incidents run / reused from cache, genes by status (observed / unavailable / failed)."""
    tag_name = {ex.tag: n for n, ex in base.REGISTRY.items()}
    out: dict[str, dict[str, Any]] = {}

    def row(n: str) -> dict[str, Any]:
        return out.setdefault(n, {"run_seconds": 0.0, "original_seconds": 0.0, "incidents_run": 0,
                                  "incidents_cached": 0, "original_unknown": 0, "observed": 0, "unavailable": 0,
                                  "failed": 0})

    for n, (sec, _genes) in ex_totals.items():
        row(n)["run_seconds"] = round(sec, 3)
    for st in ex_stats.values():
        ran = set(st.get("extractor_seconds") or {})
        orig = st.get("extractor_original_seconds") or {}
        for n in [*names, *(x for x in ran if x not in names)]:
            r = row(n)
            r["incidents_run" if n in ran else "incidents_cached"] += 1
            if n in orig:
                r["original_seconds"] = round(r["original_seconds"] + float(orig[n]), 4)
            elif n in ran:
                r["original_seconds"] = round(r["original_seconds"] + float(st["extractor_seconds"][n]), 4)
            else:
                r["original_unknown"] += 1  # cache entry written before compute times were stored
    for g in genomes.values():
        for x in g.genes:
            r = row(tag_name.get(x.extractor or "", x.extractor or "?"))
            k = str(x.status)
            if k in ("observed", "unavailable", "failed"):
                r[k] += 1
    return dict(sorted(out.items()))


SELF_ASSIGN_MODE = "leave_one_out"
# Cap of the leave-one-out pass (EXT-PROCESS 2026-11-21). Each item scores one incident against the whole run
# (cost grows with N; estimated 1-2.5 h per 10k incidents on the server), so large external corpora may cap it:
# HYDRA_SELF_ASSIGN_MAX=<n> runs the pass on a deterministic stratified subset of n incidents (members of
# multi-member campaigns round-robin over campaigns, singletons by sha256 order, each stratum in proportion to its
# share). Unset / empty / <= 0 = every incident (default, unchanged behaviour). Rates are then over the subset.
SELF_ASSIGN_MAX_ENV = "HYDRA_SELF_ASSIGN_MAX"


def self_assign_cap() -> int | None:
    v = (os.environ.get(SELF_ASSIGN_MAX_ENV) or "").strip()
    try:
        n = int(v) if v else 0
    except ValueError:
        log.warning("%s=%r is not an integer; ignored", SELF_ASSIGN_MAX_ENV, v)
        return None
    return n if n > 0 else None


def self_assign_sample(ids: list[str], campaigns: list[Campaign], cap: int | None) -> list[str]:
    """Deterministic stratified subset of `ids` of size `cap` (all of them when cap is None or not below len)."""
    if cap is None or cap >= len(ids):
        return list(ids)
    pool = set(ids)
    multi = sorted((c for c in campaigns if len(c.members) > 1), key=lambda c: c.id)
    queues = [sorted(m for m in c.members if m in pool) for c in multi]
    members = {m for q in queues for m in q}
    singles = sorted((i for i in ids if i not in members), key=lambda i: hashlib.sha256(i.encode()).hexdigest())
    n_mem = round(cap * len(members) / len(ids))
    if members and singles:
        n_mem = min(max(n_mem, 1), cap - 1)
    n_mem = min(n_mem, len(members))
    n_single = min(cap - n_mem, len(singles))
    n_mem = min(cap - n_single, len(members))
    picked: list[str] = []
    depth = 0
    while len(picked) < n_mem:
        for q in queues:
            if depth < len(q) and len(picked) < n_mem:
                picked.append(q[depth])
        depth += 1
    return sorted(picked + singles[:n_single])


def _self_assign_item(shared: tuple, iid: str):
    """One leave-one-out open-set assignment (`self_assign`); pure, so it can run in a forked worker."""
    import copy

    genomes, camps, groups, rarity, th, conformal_cal, fusion, per_query = shared
    rest = [c.model_copy(update={"members": [m for m in c.members if m != iid]}) for c in camps]
    rest = [c for c in rest if c.members]
    corpus = {k: v for k, v in genomes.items() if k != iid}
    r = copy.copy(rarity)
    r.kit_unit_of = {m: (c.group_id or c.id) for c in rest for m in c.members}
    # the copy shares the parent's caches; near-hash / MinHash frequencies depend on this item's operator units
    # and the cache key only holds their count (finding C2), the merged-unit map is keyed by dict id
    for cache in ("_near_df_cache", "_mh_df_cache", "_op_units_cache"):
        r.__dict__.pop(cache, None)
    return assign_open_set(genomes[iid], corpus, rest, groups, r, th, conformal=conformal_cal, fusion=fusion,
                           cand=per_query.get(iid, {}))


def self_assign(genomes: dict[str, IncidentGenome], clustering: Clustering, rarity: Rarity, th: Thresholds,
                conformal_cal: dict[str, Any] | None = None, fusion: FusionModel | None = None,
                run_id: str | None = None, progress: Progress | None = None
                ) -> tuple[list[Assignment], dict[str, dict], dict[str, Any]]:
    """Open-set assignment of every incident of the run against the run's own campaigns with that incident left
    out (the product path `hydra.assign.assign`, as in tests/core/test_sample_regression.py): members of a
    multi-member campaign should come back to it, singletons should be new patterns. The batch rarity (all
    incidents, incl. the held-out one) is reused; only the kit units change per incident.

    Returns (assignments, conformal info per incident, summary metrics). The rejection rate of whole held-out
    campaigns is the conformal calibration's leave-one-campaign-out pass (`conformal.calibrate`)."""

    camps, groups = clustering.campaigns, clustering.groups
    own = {m: c.id for c in camps for m in c.members}
    size = {c.id: len(c.members) for c in camps}
    out: list[Assignment] = []
    conf: dict[str, dict] = {}
    counts: dict[str, int] = {}
    decided_by: dict[str, int] = {}
    agree = members = members_back = singles = singles_new = wrong = 0
    t0 = time.perf_counter()
    from hydra import parallel
    from hydra.retrieval import candidates_per_query

    per_query = candidates_per_query(genomes, rarity)  # one retrieval pass instead of one per incident
    cap = self_assign_cap()
    ids = self_assign_sample(sorted(genomes), camps, cap)
    workers = parallel.downstream_workers(len(ids)) if len(ids) * 40 >= FORK_MIN_ITEMS else 0
    shared = (genomes, camps, groups, rarity, th, conformal_cal, fusion, per_query)
    results = parallel.fork_map(_self_assign_item, ids, shared, workers,
                                on_chunk=(lambda k: progress.set(self_assigned=k)) if progress else None)
    for n, (iid, res) in enumerate(zip(ids, results, strict=True), 1):
        a = res.assignment
        a.meta.update({"mode": SELF_ASSIGN_MODE, "base_run": run_id, "clustering_campaign": own.get(iid)})
        dec = str(a.decision)
        counts[dec] = counts.get(dec, 0) + 1
        if res.conformal:
            conf[iid] = res.conformal
            k = str(res.conformal.get("decided_by"))
            decided_by[k] = decided_by.get(k, 0) + 1
        if size.get(own.get(iid, ""), 1) > 1:
            members += 1
            ok = a.decision == "assign" and a.campaign_id == own[iid]
            members_back += ok
            wrong += a.decision == "assign" and a.campaign_id != own[iid]
        else:
            singles += 1
            ok = a.decision == "new_campaign"
            singles_new += ok
        agree += ok
        a.meta["agrees_with_clustering"] = bool(ok)
        out.append(a)
        if progress and n % 25 == 0:
            progress.set(self_assigned=n)
    # whole campaign held out: `conformal.calibrate` runs the same decision for each (capped) held-out member
    held = {"items": 0, "rejected": 0, "abstain": 0, "assigned_elsewhere": 0}
    for h in (conformal_cal or {}).get("held_out") or []:
        d = h.get("decision")
        if d is None:
            continue
        held["items"] += 1
        held["rejected" if d == "new_campaign" else "abstain" if d == "abstain" else "assigned_elsewhere"] += 1
    total = len(out)
    cal = conformal_cal or {}
    rate = lambda x, y: round(x / y, 4) if y else None  # noqa: E731
    summary = {
        "mode": SELF_ASSIGN_MODE, "model": out[0].model if out else None, "assignments": total,
        "by_decision": counts, "agreement_with_clustering": rate(agree, total), "agree": agree,
        "abstain_rate": rate(counts.get("abstain", 0), total),
        "members": members, "members_returned": members_back, "member_return_rate": rate(members_back, members),
        "members_to_other_campaign": wrong,
        "singletons": singles, "singletons_new": singles_new, "singleton_new_rate": rate(singles_new, singles),
        "decided_by": decided_by,
        "held_out_campaign_rejection_rate": rate(held["rejected"], held["items"]), "held_out": held,
        "conformal_held_out_rejection_rate": cal.get("held_out_rejection_rate"), "held_out_n": held["items"],
        "conformal_usable": cal.get("usable"), "seconds": round(time.perf_counter() - t0, 3),
        "capped": ({"cap": cap, "incidents": len(genomes), "sampled": len(ids),
                    "strategy": "stratified: campaign members round-robin, singletons sha256 order"}
                   if cap is not None and len(ids) < len(genomes) else None),
        "note_pl": ("Każdy incydent przebiegu przypisany ścieżką zbioru otwartego do kampanii tego przebiegu z"
                    " pominięciem jego samego (leave-one-out). Zgodność = członek kampanii wraca do niej, incydent"
                    " pojedynczy zostaje nowym wzorcem. Odrzucenie odłożonych kampanii: cała kampania pominięta,"
                    " jej członkowie przypisani tą samą ścieżką; poprawnie = nowy wzorzec (nie inna kampania)."),
    }
    return out, conf, summary


def load_prior(settings: Settings, spec: str, exclude: set[str] | None = None) -> BackgroundPrior | None:
    """Background document frequencies from EXTERNAL corpora only (`ext:*`, `public:*` matching `spec`); organiser
    incidents never enter a prior. Cached under <state>/priors/ by a signature of the incidents and genes used."""
    from hydra.corpus import corpus_of, is_external, matches
    from hydra.entropy import build_prior

    assert settings.state_dir is not None
    with closing(connect(settings.db_path, readonly=True)) as conn:
        ids = sorted(r["id"] for r in conn.execute("SELECT id, source FROM incidents")
                     if is_external(corpus_of(r["source"])) and matches(corpus_of(r["source"]), spec)
                     and r["id"] not in (exclude or set()))
        if not ids:
            return None
        q = ",".join("?" * len(ids))
        sig_row = conn.execute(f"SELECT COUNT(*) n, MAX(id) m FROM genes WHERE incident_id IN ({q})", ids).fetchone()
        sig = hashlib.sha256(f"{spec}|{','.join(ids)}|{sig_row['n']}|{sig_row['m']}".encode()).hexdigest()[:16]
        cache = settings.state_dir / "priors" / f"prior-{sig}.json"
        if cache.is_file():
            return BackgroundPrior.from_json(json.loads(cache.read_text(encoding="utf-8")))
        genes: dict[str, list[Gene]] = {i: [] for i in ids}
        for r in conn.execute(f"SELECT * FROM genes WHERE incident_id IN ({q}) AND status='observed'"
                              " AND value IS NOT NULL AND vector_ref IS NULL", ids):
            genes[r["incident_id"]].append(_row_gene(r))
    prior = build_prior(genes, spec)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(prior.to_json(), ensure_ascii=False), encoding="utf-8")
    return prior


def _background_prior_info() -> dict[str, Any]:
    from hydra.priors import describe

    d = describe()
    return {k: d.get(k) for k in ("enabled", "mode", "units", "incidents", "kinds", "schema")}


# Incremental analyse (SCALE 2026-11-21). Every stage after gene extraction is a pure function of its inputs, so
# a stage whose exact inputs are unchanged is reused from `<state>/stage_cache/` instead of recomputed:
#   score    (rarity, pair scores)  <- analysis code digest, digest of every gene / vector row and incident
#                                      field the genomes are built from, thresholds, fusion model file, prior
#   cluster  (campaigns, groups)    <- score key + analyst must / cannot links
#   conformal, self_assign          <- cluster key
# Typical reuse: a re-run after review decisions (constraints change: scoring is reused, clustering recomputed),
# a re-run after a crash in a late stage, an unchanged corpus. Any new or changed incident changes every document
# frequency (n is in every rarity term), so all stages are recomputed: exact equality with a full run is the
# contract (tests/perf/test_incremental.py), never an approximation. Gene extraction is incremental already (gene
# cache per incident and the CAS). Opt-in (`analyse(incremental=True)`, `hydra analyse --incremental`): the
# pickled pair scores are ~1 KB per candidate pair.
STAGE_CACHE_VERSION = 1
STAGE_CACHE_KEEP = 2  # newest entries kept per stage


@lru_cache(maxsize=1)
def analysis_code_digest() -> str:
    """sha256 over every module of the hydra package (any code change invalidates the stage cache)."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(x for x in root.rglob("*.py") if "__pycache__" not in x.parts):
        h.update(f.relative_to(root).as_posix().encode() + b"|")
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def stage_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps([STAGE_CACHE_VERSION, *parts], sort_keys=True, default=str).encode()
                          ).hexdigest()[:24]


class StageCache:
    """Pickled stage outputs under the state dir (trusted like the DB); `enabled=False` is a no-op cache."""

    def __init__(self, state_dir: Path | None, enabled: bool) -> None:
        self.root = (state_dir / "stage_cache") if (enabled and state_dir is not None) else None
        self.reused: list[str] = []

    def get(self, stage: str, key: str | None) -> Any | None:
        if self.root is None or key is None:
            return None
        import pickle

        try:
            with open(self.root / f"{stage}-{key}.pkl", "rb") as fh:
                out = pickle.load(fh)  # noqa: S301 — written by this code into the run's own state dir
        except (OSError, EOFError, pickle.UnpicklingError, AttributeError, ImportError):
            return None
        self.reused.append(stage)
        return out

    def put(self, stage: str, key: str | None, value: Any) -> None:
        if self.root is None or key is None:
            return
        import pickle

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{stage}-{key}.pkl"
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(value, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(path)
            old = sorted(self.root.glob(f"{stage}-*.pkl"), key=lambda f: f.stat().st_mtime, reverse=True)
            for f in old[STAGE_CACHE_KEEP:]:
                f.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("stage cache write failed (%s): %s", stage, exc)


def score_and_cluster(genomes: dict[str, IncidentGenome], th: Thresholds, passes: int = 1, seed: bool = False,
                      fusion: FusionModel | None = None, prior: BackgroundPrior | None = None,
                      progress: Progress | None = None, must_link: list | None = None,
                      cannot_link: list | None = None, stages: StageCache | None = None,
                      score_key: str | None = None, ignored: set[tuple[str, str]] | None = None
                      ) -> tuple[Rarity, dict[tuple[str, str], PairEval], Clustering, dict[str, Any]]:
    """Rarity units = incidents (optionally pass-0 seed units, `entropy.seed_units`). Optional extra passes use
    the campaigns of the
    previous pass as units; measured on the sample (CORE_LOG 13:45) they amplify one false merge into a giant
    component (values inside a merged unit all look unique), so the default is a single pass."""
    # Seed units are off by default: on the sample they chained {1,2,48,61} through platform values and made
    # df-4 values look unique (CORE_LOG 13:50). Incident-level df is the default.
    from hydra.entropy import apply_operator_seed_units, build_postings, duplicate_units, seed_units

    # Byte-identical incidents (same non-empty set of original files) are one rarity unit (review M9, CR-VP-1)
    unit_of: dict[str, str] | None = (seed_units({i: g.genes for i, g in genomes.items()},
                                                 build_postings({i: g.genes for i, g in genomes.items()}))
                                      if seed else duplicate_units({i: g.genes for i, g in genomes.items()}))
    info: dict[str, Any] = {}
    history: list[int] = []
    prev_sig: list[tuple[str, ...]] | None = None
    # stage reuse (incremental analyse) only in the default single pass: later passes depend on earlier campaigns
    stages = stages if (passes == 1 and not seed and score_key is not None) else None
    cluster_key = stage_key("cluster", score_key, sorted(map(tuple, must_link or [])),
                            sorted(map(tuple, cannot_link or []))) if stages else None
    for i in range(passes):
        if progress:
            progress.stage("rarity")
        hit = stages.get("score", score_key) if stages else None
        if hit is not None:
            rarity, pairs, info = hit
        else:
            rarity = rarity_for(genomes, unit_of, prior)
            rarity.ignored = set(ignored or ())  # analyst `ignore_value` reviews (ADOPT #2): 0 bits
            # operator values counted per shared-operator unit, as in assignment (finding C1, SPEC_ERRATA)
            apply_operator_seed_units(rarity, {k: g.genes for k, g in genomes.items()}, rarity.ignored)
            pairs, info = score_corpus(genomes, rarity, th, fusion=fusion, progress=progress)
            if stages:
                stages.put("score", score_key, (rarity, pairs, info))
        if progress:
            progress.stage("clustering", pairs_scored=len(pairs))
        clustering = stages.get("cluster", cluster_key) if stages else None
        if clustering is None:
            clustering = cluster(genomes, pairs, rarity, th, must_link=must_link, cannot_link=cannot_link)
            if stages:
                stages.put("cluster", cluster_key, clustering)
        if progress:
            progress.set(campaigns=len(clustering.campaigns), groups=len(clustering.groups))
        sig = sorted(tuple(c.members) for c in clustering.campaigns)
        history.append(len(clustering.campaigns))
        if prev_sig is not None and sig == prev_sig:
            break
        prev_sig = sig
        unit_of = dict(clustering.membership)
        if i == passes - 1:
            break
    info["rarity_passes"] = history
    if stages:
        info["_cluster_key"] = cluster_key
    return rarity, pairs, clustering, info


# Downstream maps (pair scores, leave-one-family-out robustness, calibration, self-assignment) fork from this
# many items on (hydra.parallel.fork_map: same functions, results in input order, identical to the loop).
FORK_MIN_ITEMS = 2000


def _score_item(shared: tuple, item: tuple) -> PairEval:
    genomes, rarity, th = shared
    (a, b), src = item
    return score_pair(genomes[a], genomes[b], rarity, th, src)


def _robust_item(shared: tuple, item: tuple) -> dict | None:
    genomes, rarity, th = shared
    (a, b), pe = item
    return robustness(genomes[a], genomes[b], rarity, pe, th)


def score_corpus(genomes: dict[str, IncidentGenome], rarity: Rarity, th: Thresholds,
                 fusion: FusionModel | None = None, progress: Progress | None = None
                 ) -> tuple[dict[tuple[str, str], PairEval], dict[str, Any]]:
    """Candidates + pair scores. `fusion` (a FusionModel fitted in an explicit train step on a held-out split,
    see `fit_fusion`) re-scores pairs; it is never fitted here on `incidents.label` (leakage)."""
    if progress:
        progress.stage("candidates")
    cand = retrieve(genomes, rarity)
    if progress:
        progress.stage("scoring", pairs_total=len(cand), pairs_scored=0)
    from hydra import parallel

    items = [(k, sorted(src)) for k, src in cand.items()]
    workers = parallel.downstream_workers(len(items)) if len(items) >= FORK_MIN_ITEMS else 0
    evals = parallel.fork_map(_score_item, items, (genomes, rarity, th), workers,
                              on_chunk=(lambda n: progress.set(pairs_scored=n)) if progress else None)
    pairs: dict[tuple[str, str], PairEval] = dict(zip((k for k, _ in items), evals, strict=True))
    info: dict[str, Any] = {"candidate_pairs": len(cand), "scorer": "rarity_bits@1", "score_workers": workers}
    linked = [(k, pe) for k, pe in pairs.items() if is_linked(pe, th)]
    rob_workers = parallel.downstream_workers(len(linked)) if len(linked) >= FORK_MIN_ITEMS // 10 else 0
    for ((_a, _b), pe), rob in zip(linked, parallel.fork_map(_robust_item, linked, (genomes, rarity, th),
                                                             rob_workers), strict=True):
        pe.robustness = rob
    camp = [pe for _, pe in linked if pe.robustness and pe.robustness["link"] == "campaign"]
    info["robustness"] = {"linked_pairs": len(linked), "campaign_links": len(camp),
                          "robust_campaign_links": sum(1 for pe in camp if pe.robustness["robust_link"]),
                          "single_family_campaign_links": sum(1 for pe in camp if not pe.robustness["robust_link"])}
    if fusion is not None and fusion._lr is not None:
        for pe in pairs.values():
            fusion.apply(pe, th)
        info["scorer"] = fusion.name
    return pairs, info


def fit_fusion(train: dict[str, IncidentGenome], labels: dict[str, str], th: Thresholds | None = None,
               min_pairs: int = 30) -> FusionModel | None:
    """Explicit train step (EVAL): fit LR + isotonic on labelled pairs of a *training* split only."""
    th = th or Thresholds()
    rarity = rarity_for(train)
    pairs, _ = score_corpus(train, rarity, th)
    lab = [(pe, int(labels[a] == labels[b])) for (a, b), pe in pairs.items() if a in labels and b in labels]
    fm = FusionModel(min_pairs=min_pairs)
    return fm if fm.fit([p for p, _ in lab], [y for _, y in lab]) else None


# --------------------------------------------------------------------------------------- persistence


def _robust_fields(pe: PairEval) -> dict[str, Any]:
    """Extra top-level keys in `pairs.detail`. They are not PairScore fields and PairScore forbids extras: read
    the stored JSON back with `pair_score_from_detail` (CR-CORE-4 asks for a `PairScore.meta` field)."""
    return {"robustness": pe.robustness, "robust_link": pe.robustness["robust_link"]} if pe.robustness else {}


DETAIL_EXTRA_KEYS = {"campaign_bits", "kit_bits", "asset_bits", "campaign_by_family", "operator_bits", "support",
                     "robustness", "robust_link", "conformal", "kit_groups", "kit_min_df"}


def pair_score_from_detail(detail: dict[str, Any]) -> PairScore:
    return PairScore.model_validate({k: v for k, v in detail.items() if k not in DETAIL_EXTRA_KEYS})


def assignment_from_detail(detail: dict[str, Any]) -> Assignment:
    return Assignment.model_validate({k: v for k, v in detail.items() if k not in DETAIL_EXTRA_KEYS})


def _write_run(conn: sqlite3.Connection, run_id: str, kind: str, params: dict, metrics: dict,
               pairs: dict[tuple[str, str], PairEval], clustering: Clustering | None,
               assignments: list[Assignment] | None = None, created_at: str | None = None,
               conformal: dict[str, dict] | None = None) -> None:
    conn.execute(
        "INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, kind, events.canonical_json(params), events.canonical_json(metrics), created_at or events.utcnow(),
         events.utcnow()),
    )
    conn.executemany(
        "INSERT INTO pairs (a, b, run_id, total_bits, probability, calibrated, hypothesis, model, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(pe.score.a, pe.score.b, run_id, pe.score.total_bits, pe.score.probability, int(pe.score.calibrated),
          pe.score.hypothesis, pe.score.model,
          events.canonical_json(redact_obj({**pe.score.model_dump(), "campaign_bits": pe.campaign_bits,
                                            "kit_bits": pe.kit_bits, "asset_bits": pe.asset_bits,
                                            "campaign_by_family": pe.campaign_by_family,
                                            "operator_bits": pe.operator_bits, "support": pe.support,
                                            "kit_groups": pe.kit_groups, "kit_min_df": pe.kit_min_df,
                                            **_robust_fields(pe)})))
         for pe in pairs.values()],
    )
    if clustering is not None:
        for c in clustering.campaigns:
            conn.execute(
                "INSERT INTO campaigns (id, run_id, label, confidence, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (c.id, run_id, redact_text(c.label), c.confidence, c.status,
                 events.canonical_json(redact_obj(c.model_dump()))),
            )
            conn.executemany("INSERT INTO campaign_members (run_id, campaign_id, incident_id) VALUES (?, ?, ?)",
                             [(run_id, c.id, m) for m in c.members])
        conn.executemany(
            "INSERT INTO campaign_groups (run_id, id, label, confidence, detail) VALUES (?, ?, ?, ?, ?)",
            [(run_id, g.id, redact_text(g.label), g.confidence,
              events.canonical_json(redact_obj({**g.model_dump(), "meta": clustering.group_meta.get(g.id, {})})))
             for g in clustering.groups])
    for a in assignments or []:
        conn.execute(
            "INSERT INTO assignments (incident_id, run_id, decision, campaign_id, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (a.incident_id, run_id, a.decision, a.campaign_id,
             events.canonical_json(redact_obj({**a.model_dump(), **({"conformal": conformal[a.incident_id]}
                                                                   if conformal and a.incident_id in conformal
                                                                   else {})})),
             events.utcnow()),
        )
    events.append(conn, "RUN_FINISHED", {"run_id": run_id, "kind": kind, "pairs": len(pairs),
                                         "campaigns": len(clustering.campaigns) if clustering else 0})


# --------------------------------------------------------------------------------------- entry points


def import_paths_as_incidents(paths: list[Path], writer: Writer, store: ContentStore, settings: Settings,
                              prefix: str = "nask", skipped: list[str] | None = None) -> list[Incident]:
    from hydra.adapters.datasets import CORPUS_FILE, import_corpus

    incidents: list[Incident] = []
    for p in paths:
        p = Path(p).expanduser().resolve()
        if p.is_dir() and (p / CORPUS_FILE).is_file():
            # a materialised external corpus: its own source/labels (ext:<name>), never nask-sample
            incidents.extend(r.incident for r in import_corpus(p, settings, writer=writer, store=store))
            continue
        _refuse_inside_external_corpus(p)
        if skipped is not None:
            skipped.extend(f"{p.name}/{n}" for n in loose_root_files(p))
        for folder in discover_incidents(p, settings):  # CR-ADAPT-1: ZIP scratch under the state dir
            if folder.is_file():
                incidents.append(import_single(folder, writer, store, settings).incident)
            else:
                incidents.append(import_incident(folder, writer, store, settings, prefix=prefix).incident)
    return incidents


def analyse(paths: list[Path], settings: Settings, *, only: list[str] | None = None, use_cache: bool = True,
            thresholds: Thresholds | None = None, writer: Writer | None = None,
            options: dict | None = None, prefix: str = "nask", fusion: FusionModel | None = None,
            corpus: str | None = None, prior: str | None = None, fusion_path: Path | None = None,
            job_id: str | None = None, incremental: bool = False) -> RunResult:
    """One analyse run.

    `incremental`: reuse every post-extraction stage whose exact inputs are unchanged (`StageCache`); the result
    equals a full run.
    `corpus` (hydra.corpus spec): None = today's behaviour, the incidents imported from `paths`, restricted to the
    NASK mode (nask + upload; external incidents are never mixed in by default). A spec (`nask`, `extended`, `all`,
    `ext:<name>`, ...) also pulls every stored incident of those corpora; `paths` may then be empty. Rarity,
    candidates and clustering only ever see the run's incidents.
    `prior`: external corpora (spec) whose document frequencies become a rarity prior (opt-in).
    `fusion_path`: a model written by `hydra train` (opt-in; verified against its provenance file).
    Progress: a `jobs` row + `<state>/progress/<job_id>.json` (`hydra.progress.run_progress`)."""
    from hydra.corpus import corpus_of, incidents_in, matches

    th = thresholds or Thresholds()
    settings.ensure_dirs()
    own_writer = writer is None
    writer = writer or Writer(settings.db_path)
    store = ContentStore(settings.cas_dir)
    run_id = new_run_id("analyse")
    started = events.utcnow()
    timings: dict[str, float] = {}
    job_id = job_id or create_job(writer, "analyse", {"paths": [str(p) for p in paths], "corpus": corpus,
                                                       "run_id": run_id})
    progress = Progress(settings, writer, job_id, run_id=run_id, corpus=corpus or "nask")
    try:
        t = time.perf_counter()
        skipped: list[str] = []
        imported = import_paths_as_incidents(paths, writer, store, settings, prefix=prefix, skipped=skipped)
        with closing(connect(settings.db_path, readonly=True)) as conn:
            wanted = [i.id for i in imported]
            if corpus is not None:
                wanted = sorted(set(wanted) | set(incidents_in(conn, corpus)))
            # reload from DB so cached fingerprints from earlier runs are visible
            incidents = [x for i in dict.fromkeys(wanted) if (x := load_incident(conn, i)) is not None]
        excluded = [i.id for i in incidents if not matches(corpus_of(i.source), corpus)]
        incidents = [i for i in incidents if matches(corpus_of(i.source), corpus)]
        timings["import"] = time.perf_counter() - t

        progress.stage("genes", incidents_total=len(incidents), incidents_done=0)
        t = time.perf_counter()
        ex_totals: dict[str, list[float]] = {}

        def _ex_time(name: str, part: base.ExtractResult, sec: float) -> None:
            tot = ex_totals.setdefault(name, [0.0, 0.0])
            tot[0] += sec
            tot[1] += len(part.genes)
            progress.state["extractors"][name] = {"seconds": round(tot[0], 3), "genes": int(tot[1])}

        ex_stats = extract(incidents, writer, store, settings, only=only, use_cache=use_cache, options=options,
                           on_done=lambda inc, res: progress.incident_done(res.genes if res else None),
                           on_extractor=_ex_time)
        _backfill_original_seconds(settings, ex_stats, list(only or base.REGISTRY))
        timings["genes"] = time.perf_counter() - t

        t = time.perf_counter()
        ids = [i.id for i in incidents]
        gdigest = hashlib.sha256() if incremental else None
        with closing(connect(settings.db_path, readonly=True)) as conn:
            genomes = load_genomes(conn, ids, digest=gdigest)
        fam_counts: dict[str, int] = {}
        for g in genomes.values():
            for x in g.genes:
                fam_counts[str(x.family)] = fam_counts.get(str(x.family), 0) + 1
        progress.set(genes_by_family=dict(sorted(fam_counts.items())), genes=sum(fam_counts.values()))
        timings["load_genomes"] = time.perf_counter() - t

        bg = load_prior(settings, prior, exclude=set(ids)) if prior else None
        if fusion is None and fusion_path is not None:
            from hydra.train import load_fusion

            fusion = load_fusion(Path(fusion_path), settings)
        t = time.perf_counter()
        from hydra.review import ignored_values, pair_constraints

        with closing(connect(settings.db_path, readonly=True)) as conn:
            must, cannot = pair_constraints(conn, set(ids))
            ignored = ignored_values(conn)
        stages = StageCache(settings.state_dir, incremental)
        score_key = None
        if incremental and gdigest is not None and (fusion is None or fusion_path is not None):
            fusion_id = hashlib.sha256(Path(fusion_path).read_bytes()).hexdigest() if fusion_path else None
            prior_id = (hashlib.sha256(json.dumps(bg.to_json(), sort_keys=True).encode()).hexdigest()
                        if bg else None)
            from hydra.entropy import operator_units_mode
            from hydra.priors import fingerprint as shipped_prior_id  # shipped prior + HYDRA_BACKGROUND_PRIOR mode

            # HYDRA_OPERATOR_UNITS (SCORE-GROWTH); always in the key so caches from the legacy era are not reused
            mode = operator_units_mode()
            score_key = stage_key("score", analysis_code_digest(), gdigest.hexdigest(), ids, th.__dict__,
                                  fusion_id, prior_id, sorted(ignored), shipped_prior_id(), "operator_units=" + mode)
        rarity, pairs, clustering, score_info = score_and_cluster(genomes, th, fusion=fusion, prior=bg,
                                                                  progress=progress, must_link=must,
                                                                  cannot_link=cannot, stages=stages,
                                                                  score_key=score_key, ignored=ignored)
        cluster_key = score_info.pop("_cluster_key", None)
        score_info["analyst_constraints"] = {"must_link": len(must), "cannot_link": len(cannot),
                                             "ignored_values": len(ignored)}
        timings["rarity_scoring_clustering"] = time.perf_counter() - t

        progress.stage("assign")
        t = time.perf_counter()
        from hydra.conformal import calibrate

        conf_key = stage_key("conformal", cluster_key) if cluster_key else None
        conformal_cal = stages.get("conformal", conf_key)
        if conformal_cal is None:
            conformal_cal = calibrate(genomes, clustering.campaigns, th)
            stages.put("conformal", conf_key, conformal_cal)
        timings["conformal_calibration"] = time.perf_counter() - t
        t = time.perf_counter()
        assign_key = stage_key("self_assign", conf_key, self_assign_cap()) if conf_key else None
        hit = stages.get("self_assign", assign_key)
        if hit is not None:
            self_assignments, self_conf, assign_summary = hit
            for a in self_assignments:  # the only run-specific field
                a.meta["base_run"] = run_id
        else:
            self_assignments, self_conf, assign_summary = self_assign(genomes, clustering, rarity, th,
                                                                      conformal_cal, fusion, run_id, progress)
            stages.put("self_assign", assign_key, (self_assignments, self_conf, assign_summary))
        timings["self_assignment"] = time.perf_counter() - t
        hyp_counts: dict[str, int] = {}
        for pe in pairs.values():
            hyp_counts[str(pe.score.hypothesis)] = hyp_counts.get(str(pe.score.hypothesis), 0) + 1
        corpora: dict[str, int] = {}
        for i in incidents:
            corpora[corpus_of(i.source)] = corpora.get(corpus_of(i.source), 0) + 1
        metrics = {
            "incidents": len(ids), "genes": sum(len(g.genes) for g in genomes.values()),
            "pairs_scored": len(pairs), "hypotheses": hyp_counts, "campaigns": len(clustering.campaigns),
            "multi_member_campaigns": sum(1 for c in clustering.campaigns if len(c.members) > 1),
            "campaign_groups": [redact_obj({**g.model_dump(), "meta": clustering.group_meta.get(g.id, {})})
                                for g in clustering.groups],
            "alerts": clustering.alerts, "extract": ex_stats, "conformal": conformal_cal,
            "assignment": assign_summary, "extractors": extractor_summary(ex_stats, ex_totals, genomes,
                                                              list(only or base.REGISTRY)),
            "loose_root_files": skipped, "corpora": corpora, "excluded_by_corpus_flag": excluded,
            "prior": ({"corpus": bg.corpus, "incidents": bg.n} if bg else None),
            "background_prior": _background_prior_info(), **score_info,
            "timings_s": {k: round(v, 3) for k, v in timings.items()},
        }
        if incremental:
            metrics["incremental"] = {"stages_reused": stages.reused, "score_key": score_key}
        params = {"paths": [str(p) for p in paths], "only": only, "thresholds": th.__dict__,
                  "corpus": corpus or "nask", "prior": prior, "job_id": job_id,
                  "fusion": str(fusion_path) if fusion_path else (fusion.name if fusion else None),
                  "extractors": [base.REGISTRY[n].tag for n in (only or list(base.REGISTRY))],
                  "extract_versions": extract_versions(settings, list(only or base.REGISTRY), options)}
        t = time.perf_counter()
        writer.run(lambda conn: _write_run(conn, run_id, "analyse", params, metrics, pairs, clustering,
                                           self_assignments, created_at=started, conformal=self_conf),
                   timeout=600)
        timings["persist"] = time.perf_counter() - t
        metrics["timings_s"]["persist"] = round(timings["persist"], 3)
        # DATAFLOW (ADR-0009): materialise the run's aggregates (computed on a reader, installed by the Writer)
        from hydra.dataflow import build_safely

        t = time.perf_counter()
        build_safely(settings.db_path, writer, run_id)
        timings["aggregates"] = time.perf_counter() - t
        metrics["timings_s"]["aggregates"] = round(timings["aggregates"], 3)
        # API-PERF (CR-GRAPH2-2): precompute the UI views (graph base, flow, dashboard, analytics) before the run
        # is reported done, so the first request on a new run reads the disk cache. Best effort, never raises.
        t = time.perf_counter()
        from hydra.api_cache import precompute_run

        precompute_run(settings, run_id)
        timings["api_cache"] = time.perf_counter() - t
        progress.finish("done", campaigns=len(clustering.campaigns), groups=len(clustering.groups),
                        pairs_scored=len(pairs))
        res = RunResult(run_id, ids, genomes, rarity, pairs, clustering, timings, metrics)
        res.job_id = job_id
        return res
    except BaseException as exc:
        try:
            progress.finish("failed", error=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:  # noqa: BLE001 — never mask the original error
            pass
        raise
    finally:
        if own_writer:
            writer.close()


def analyse_corpus(paths: list[Path], settings: Settings, writer: Writer, **kwargs: Any) -> RunResult:
    """Build / rebuild the corpus from dataset folders inside a running process (API `{path}` / folder upload):
    uses the caller's single Writer."""
    return analyse([Path(p) for p in paths], settings, writer=writer, **kwargs)


def _group_from_detail(d: dict[str, Any]) -> CampaignGroup:
    """CampaignGroup has no meta field (CR-CORE-6): the stored JSON carries `meta` as an extra key."""
    return CampaignGroup.model_validate({k: v for k, v in d.items() if k != "meta"})


def latest_run(conn: sqlite3.Connection, kind: str = "analyse") -> str | None:
    row = conn.execute("SELECT id FROM runs WHERE kind = ? ORDER BY created_at DESC LIMIT 1", (kind,)).fetchone()
    return row["id"] if row else None


def load_clustering(conn: sqlite3.Connection, run_id: str) -> tuple[list[Campaign], list[CampaignGroup]]:
    campaigns = [Campaign.model_validate(json.loads(r["detail"]))
                 for r in conn.execute("SELECT detail FROM campaigns WHERE run_id = ?", (run_id,))]
    groups = [_group_from_detail(json.loads(r["detail"]))
              for r in conn.execute("SELECT detail FROM campaign_groups WHERE run_id = ?", (run_id,))]
    if groups:
        return campaigns, groups
    row = conn.execute("SELECT metrics FROM runs WHERE id = ?", (run_id,)).fetchone()
    groups = [_group_from_detail(g) for g in json.loads(row["metrics"]).get("campaign_groups", [])] if row \
        else []
    return campaigns, groups


NO_FEATURES_PL = ("Wstrzymano decyzję: z materiału nie odczytano żadnej cechy do porównania (pusty folder, plik"
                  " uszkodzony albo format nieobsługiwany). Szczegóły w raporcie wejścia (status każdego pliku).")


def _informative(g: IncidentGenome) -> bool:
    """At least one observed gene beyond the file hash (the hash alone still links exact duplicates)."""
    return any(x.status == GeneStatus.OBSERVED and x.kind != "artifact_sha256" for x in g.genes)

# Base corpus of open-set assignment, kept between items (assign-batch, repeated uploads): loading every base
# genome, the footage index and the base-run rows took ~10-14 s per item at 825 base incidents (SCALE). The
# entry is keyed by the state DB, the base run, the corpus ids and a per-incident signature (gene count and
# max gene id, incident title / observed_at / label), so any re-extraction or edit of a base incident reloads
# it. `annotate_footage` writes into the base genomes for each new item: the base-only values are restored
# before every item, and the lock keeps concurrent assignments (API threads) off the shared genomes.
# HYDRA_ASSIGN_BASE_CACHE=0 turns it off.
_BASE_MEMO: dict[str, Any] = {}
_BASE_LOCK = threading.RLock()


def _corpus_signature(conn: sqlite3.Connection, ids: list[str]) -> str:
    h = hashlib.sha256()
    for s in range(0, len(ids), 500):
        chunk = ids[s: s + 500]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT incident_id, COUNT(*), MAX(id) FROM genes WHERE incident_id IN ({q})"
                              " GROUP BY incident_id ORDER BY incident_id", chunk):
            h.update(repr(tuple(r)).encode())
        for r in conn.execute(f"SELECT id, title, observed_at, label FROM incidents WHERE id IN ({q}) ORDER BY id",
                              chunk):
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def _base_genomes(conn: sqlite3.Connection, settings: Settings, base_run: str | None, corpus_ids: list[str]
                  ) -> dict[str, IncidentGenome]:
    """The base genomes as `load_genomes` returns them (memoised; call under `_BASE_LOCK`)."""
    if os.environ.get("HYDRA_ASSIGN_BASE_CACHE", "1") == "0":
        return load_genomes(conn, corpus_ids)
    key = hashlib.sha256(json.dumps([str(settings.db_path), base_run, corpus_ids]).encode()).hexdigest()
    sig = _corpus_signature(conn, corpus_ids)
    if _BASE_MEMO.get("key") == key and _BASE_MEMO.get("sig") == sig:
        genomes = _BASE_MEMO["genomes"]
        for (iid, uid), v in _BASE_MEMO["footage"].items():  # base-only footage frequencies, as loaded
            if v is None:
                genomes[iid].units[uid].pop("footage_df", None)
            else:
                genomes[iid].units[uid]["footage_df"] = v
        return genomes
    genomes = load_genomes(conn, corpus_ids)
    footage = {(iid, uid): d.get("footage_df") for iid, g in genomes.items() for uid, d in g.units.items()}
    _BASE_MEMO.clear()
    _BASE_MEMO.update(key=key, sig=sig, genomes=genomes, footage=footage)
    return genomes


def _assign_incident(inc_id: str, settings: Settings, writer: Writer, store: ContentStore, *, base_run: str | None,
                     only: list[str] | None, th: Thresholds, item: str, extra: dict | None = None,
                     progress: Progress | None = None, meta: dict | None = None) -> tuple[str, Assignment]:
    """`extra` (optional dict) receives `conformal` (prediction set, p-values) for callers that show it.
    `meta` (optional) is merged into the stored `Assignment.meta` (e.g. `duplicate_of` of a re-upload).
    `progress` (INPUT_STAGES) is advanced through genes → enrich → candidates → scoring → decision."""
    with closing(connect(settings.db_path, readonly=True)) as conn:
        base_run = base_run or latest_run(conn)
        # first item on a fresh state (audit A10): no exception after the job started; it becomes a candidate
        # first pattern, published only by an analyst review (hydra.review.apply_review)
        campaigns, groups = load_clustering(conn, base_run) if base_run else ([], [])
        inc = load_incident(conn, inc_id)
        mrow = conn.execute("SELECT metrics, params FROM runs WHERE id = ?", (base_run,)).fetchone() \
            if base_run else None
        conformal_cal = json.loads(mrow["metrics"]).get("conformal") if mrow else None
        base_params = json.loads(mrow["params"]) if mrow else {}
    assert inc is not None
    if progress:
        progress.stage("genes", incident_id=inc.id, incidents_total=1, incidents_done=0)

    def _on_extractor(name: str, part: base.ExtractResult, seconds: float) -> None:
        if progress is None:
            return
        if name == "enrich":
            progress.stage("enrich")
        fams: dict[str, int] = {}
        for g in part.genes:
            fams[str(g.family)] = fams.get(str(g.family), 0) + 1
        progress.extractor_done(name, len(part.genes), seconds, fams)

    extract([inc], writer, store, settings, only=only, on_extractor=_on_extractor if progress else None,
            on_done=(lambda _i, _r: progress.set(incidents_done=1)) if progress else None)
    res, root_params, fusion = _assign_loaded(inc.id, settings, base_run, campaigns, groups, conformal_cal,
                                             base_params, th, progress=progress)
    res.assignment.meta["scoring"] = {"prior": root_params.get("prior"), "fusion": root_params.get("fusion"),
                                      "scorer": fusion.name if fusion else "rarity_bits@1"}
    run_id = new_run_id("assign")
    res.assignment.meta.setdefault("job_id", progress.job_id if progress else None)
    res.assignment.meta["base_run"] = base_run
    if meta:
        res.assignment.meta.update(meta)
    if base_run is None:
        res.assignment.meta["onboarding"] = True
        res.assignment.reason_pl = (
            "Brak przeanalizowanego korpusu: to pierwszy materiał. Cechy zostały wyodrębnione i zapisane. Aby"
            " porównywać kolejne materiały, uruchom analizę zbioru (np. próbki NASK) albo zatwierdź ten materiał"
            " jako pierwszy wzorzec (przegląd analityka: „przenieś do nowej kampanii”).")
    writer.run(lambda conn: _write_run(conn, run_id, "assign", {"base_run": base_run, "item": redact_text(item)},
                                       {"decision": res.assignment.decision, "conformal": res.conformal},
                                       res.pairs, None, [res.assignment],
                                       conformal={inc.id: res.conformal} if res.conformal else None),
               timeout=600)
    if extra is not None:
        extra["conformal"] = res.conformal
    if progress:
        progress.finish("done", run_id=run_id, decision=res.assignment.decision,
                        campaign_id=res.assignment.campaign_id, group_id=res.assignment.group_id)
    return run_id, res.assignment


def assign_stored(inc_id: str, settings: Settings, *, base_run: str | None = None,
                  th: Thresholds | None = None) -> Any:
    """Re-decide an incident whose genes are already stored (no extraction, nothing written): the same steps as
    `_assign_incident` after extraction. Used by `eval.hidden_sim rescore` to measure scoring changes."""
    with closing(connect(settings.db_path, readonly=True)) as conn:
        base_run = base_run or latest_run(conn)
        campaigns, groups = load_clustering(conn, base_run) if base_run else ([], [])
        mrow = conn.execute("SELECT metrics, params FROM runs WHERE id = ?", (base_run,)).fetchone() \
            if base_run else None
    conformal_cal = json.loads(mrow["metrics"]).get("conformal") if mrow else None
    base_params = json.loads(mrow["params"]) if mrow else {}
    return _assign_loaded(inc_id, settings, base_run, campaigns, groups, conformal_cal, base_params,
                          th or Thresholds())[0]


def _assign_loaded(inc_id: str, settings: Settings, base_run: str | None, campaigns: list[Campaign],
                   groups: list[CampaignGroup], conformal_cal: dict | None, base_params: dict, th: Thresholds,
                   progress: Progress | None = None) -> tuple[Any, dict, Any]:
    """Open-set decision for an incident whose genes are stored, against the base run campaigns.
    Returns (AssignResult, root run params, fusion model or None)."""

    corpus_ids = sorted({m for c in campaigns for m in c.members} - {inc_id})
    with _BASE_LOCK:  # the memoised base genomes are shared: one assignment at a time uses them
        with closing(connect(settings.db_path, readonly=True)) as conn:
            genomes = _base_genomes(conn, settings, base_run, corpus_ids)
            new = load_genomes(conn, [inc_id])[inc_id]
        annotate_footage({**genomes, inc_id: new})  # footage frequency of the new incident's frames vs the corpus
        campaigns = [c.model_copy(update={"members": [m for m in c.members if m != inc_id]}) for c in campaigns]
        campaigns = [c for c in campaigns if c.members]
        # Campaign-level rarity stays incident-level, as in `analyse` (campaign/group units made umbrella values
        # look unique once a sibling campaign was held out: EVAL 2026-11-21). Kit-level genes count campaign
        # groups as one unit, so a kit shared by related campaigns still ties a new incident to their group.
        # the same prior and fusion model as the batch run that built the campaigns (audit §7.3; variants-pipe
        # `scoring_context`, shared with the batch cross-comparison)
        root_params, prior, fusion = scoring_context(settings, base_run, base_params, set(genomes) | {inc_id})
        rarity = rarity_for({**genomes, inc_id: new}, prior=prior)
        rarity.kit_unit_of = {m: (c.group_id or c.id) for c in campaigns for m in c.members}
        if progress and progress.state["stage"] == "genes":
            progress.stage("enrich")  # no enrichment records for this input: the stage is passed through
        res = assign_open_set(new, genomes, campaigns, groups, rarity, th, conformal=conformal_cal, progress=progress,
                              fusion=fusion, cannot_link=active_cannot_links(settings, base_params, set(genomes)))
    if base_run is not None and not _informative(new) and res.assignment.decision != "assign":
        # nothing comparable was read (empty folder, corrupt or unsupported file): not a "new pattern"
        # (feat/inputs; here so `assign_stored` re-decides it the same way)
        res.assignment.decision = AssignmentDecision.ABSTAIN
        res.assignment.campaign_id, res.assignment.group_id = None, None
        res.assignment.reason_pl = NO_FEATURES_PL
        res.assignment.meta["no_features"] = True
    return res, root_params, fusion


def scoring_context(settings: Settings, base_run: str | None, base_params: dict, exclude: set[str]
                    ) -> tuple[dict, BackgroundPrior | None, FusionModel | None]:
    """The same prior and fusion model as the batch run that built the campaigns (audit §7.3): returns
    (root run params, prior, fusion). Shared by single-item assignment and the batch cross-comparison."""
    root_params = base_params
    if base_params.get("root_run") and base_params.get("root_run") != base_run:
        with closing(connect(settings.db_path, readonly=True)) as conn:
            rr = conn.execute("SELECT params FROM runs WHERE id = ?", (base_params["root_run"],)).fetchone()
        root_params = json.loads(rr["params"]) if rr else base_params
    prior = load_prior(settings, root_params["prior"], exclude=exclude) if root_params.get("prior") else None
    fusion = None
    if root_params.get("fusion") and str(root_params["fusion"]).endswith(".pkl"):
        from hydra.train import load_fusion

        fusion = load_fusion(Path(root_params["fusion"]), settings)
    return root_params, prior, fusion


def active_cannot_links(settings: Settings, base_params: dict, ids: set[str]) -> list[tuple[str, str]]:
    """Analyst cannot-links in force for the base revision: its resolved constraints plus pair-level
    reject_link reviews (`review.pair_constraints`) between corpus incidents."""
    from hydra.review import pair_constraints

    out = {tuple(sorted((str(x["a"]), str(x["b"])))) for x in base_params.get("constraints") or []
           if x.get("kind") == "cannot"}
    with closing(connect(settings.db_path, readonly=True)) as conn:
        out |= {tuple(sorted(p)) for p in pair_constraints(conn, ids)[1]}
    return sorted(p for p in out if p[0] in ids and p[1] in ids)  # type: ignore[misc]


def assign_new(item: str | Path, settings: Settings, *, base_run: str | None = None, only: list[str] | None = None,
               thresholds: Thresholds | None = None, writer: Writer | None = None,
               incident_id: str | None = None) -> tuple[str, Assignment]:
    """CLI entry: exactly `analyse_input` (one dispatcher for every path, audit A02)."""
    out = analyse_input(detect_input_kind(item), item, settings, base_run=base_run, only=only,
                        thresholds=thresholds, writer=writer, incident_id=incident_id)
    return out["run_id"], Assignment.model_validate(out["assignment"])


# --------------------------------------------------------------------------------------- analyse_input

INPUT_KINDS = ("url", "screenshot", "hint", "file")
URL_GENE_KINDS = ("url_in_text", "domain_in_text", "url", "ocr_url")


def _fetch_into_folder(url: str, settings: Settings) -> tuple[Path, dict[str, Any]]:
    """Isolated no-JS fetch (GENES-INF `hydra.enrich.fetch.fetch_url`; live only with HYDRA_ENRICH=1, else
    replay of a cached fetch). Returns a folder with the URL hint and whatever evidence the fetch produced."""
    import tempfile

    folder = Path(tempfile.mkdtemp(dir=settings.tmp_dir, prefix="fetch-"))
    (folder / "input.url.txt").write_text(url + "\n", encoding="utf-8")
    info: dict[str, Any] = {"url": redact_text(url), "status": GeneStatus.UNAVAILABLE.value,
                            "status_pl": "Strona niedostępna; analiza tylko z cech adresu URL."}
    try:
        from hydra.enrich.fetch import fetch_url  # type: ignore[import-not-found]
    except ImportError:
        info["reason"] = "fetcher module not installed"
        return folder, info
    try:
        res = fetch_url(url, settings)
    except Exception as exc:  # noqa: BLE001 — unreachable page = unavailable, never an error
        info["reason"] = f"{type(exc).__name__}"
        return folder, info
    if not res.hops and res.har_path and Path(res.har_path).is_file():
        res.hops = _hops_from_har(Path(res.har_path))  # replay: the cached HAR carries the chain and the page
        info["replayed"] = True
    info.update({"status": res.status, "final_url": redact_text(res.final_url),
                 "hops": len(res.hops), "white_page": list(res.white_page)})
    if res.har_path and Path(res.har_path).is_file():
        (folder / "fetch.har").write_bytes(Path(res.har_path).read_bytes())
    if res.hops and res.hops[-1].body and "html" in (res.hops[-1].mime or "html"):
        (folder / "landing.html").write_bytes(res.hops[-1].body)
    if res.favicon:
        (folder / "landing_files").mkdir(exist_ok=True)
        (folder / "landing_files" / "favicon.ico").write_bytes(res.favicon)
    info["redirect_chain"] = [redact_text(h.url) for h in res.hops][:12]
    if res.status != "ok":
        # contract status: anything but a successful fetch is GeneStatus.UNAVAILABLE; the cause stays as a note
        info["note"] = res.status
        info["status"] = GeneStatus.UNAVAILABLE.value
        info["reason"] = "live fetch disabled (HYDRA_ENRICH=0) and no cached fetch" if res.status == "disabled" \
            else res.status
        info["status_pl"] = (FETCH_OFFLINE_PL if res.status == "disabled"
                             else f"Strona niedostępna: {_fetch_cause_pl(res.status)}; analiza tylko z cech adresu"
                                  " URL.")
    else:
        info["status"] = GeneStatus.OBSERVED.value
        info["status_pl"] = ("Strona pobrana w izolacji (bez JavaScriptu i ciasteczek); liczba kroków łańcucha"
                             f" przekierowań: {len(res.hops)}; HTML i log sieci zapisane jako dowody.")
    info["screenshot"] = "not_available"  # no sandboxed headless browser in this build: no rendering, no JS
    return folder, info


def _hops_from_har(har: Path) -> list[Any]:
    """Document entries of a cached fetch HAR (hydra.enrich.fetch writes them) back into `Hop`s: URL, status, MIME
    and the decoded body, so a replayed fetch gives the same landing HTML as the live one."""
    from hydra.enrich.fetch import Hop

    try:
        entries = json.loads(har.read_text(encoding="utf-8"))["log"]["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    hops = []
    for e in entries:
        if e.get("_resourceType") != "document":
            continue
        c = (e.get("response") or {}).get("content") or {}
        text = c.get("text") or ""
        try:
            body = base64.b64decode(text) if c.get("encoding") == "base64" else text.encode("utf-8")
        except ValueError:
            body = b""
        hops.append(Hop(url=(e.get("request") or {}).get("url", ""), status=(e.get("response") or {}).get("status"),
                        mime=c.get("mimeType") or "", body=body, ip=e.get("serverIPAddress"),
                        redirect_to=(e.get("response") or {}).get("_redirect_to")))
    return hops


def _fetch_cause_pl(status: str) -> str:
    s = status.lower()
    if "gaierror" in s or "nxdomain" in s or "name or service" in s:
        return "nazwa domeny nie rozwiązuje się w DNS (domena wyłączona lub nieistniejąca)"
    if "timeout" in s or "timed out" in s:
        return "przekroczony czas odpowiedzi"
    if s.startswith("blocked:"):
        return f"adres zablokowany przez zabezpieczenie (adres prywatny lub niedozwolony: {status[8:]})"
    if "ssl" in s or "certificate" in s:
        return f"błąd TLS ({status})"
    return status


FETCH_OFFLINE_PL = ("Strona niedostępna offline: pobieranie na żywo jest wyłączone (HYDRA_OFFLINE=1 lub brak"
                    " HYDRA_ENRICH=1) i nie ma zapisanej kopii; analiza tylko z cech adresu URL.")


def search_hint(hint: str, settings: Settings, *, base_run: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Keyword search over corpus genes (phrases, names, brands, domains, titles) → ranked incidents and
    campaigns. Matching is case- and diacritic-insensitive; each hit weighs by its rarity bits."""
    import unicodedata

    def fold(t: str) -> str:
        t = unicodedata.normalize("NFKD", t.casefold().replace("ł", "l"))
        return "".join(c for c in t if not unicodedata.combining(c))

    terms = [fold(t) for t in hint.split() if len(t) >= 3] or [fold(hint)]
    with closing(connect(settings.db_path, readonly=True)) as conn:
        base_run = base_run or latest_run(conn)
        campaigns, _groups = load_clustering(conn, base_run) if base_run else ([], [])
        ids = sorted({m for c in campaigns for m in c.members}) or [
            r["id"] for r in conn.execute("SELECT id FROM incidents")]
        genomes = load_genomes(conn, ids)
    rarity = rarity_for(genomes)
    hits: dict[str, list[dict[str, Any]]] = {}
    for iid, g in genomes.items():
        cands = [(k, v) for (k, v) in g.discrete] + ([("title", g.title)] if g.title else [])
        for kind, value in cands:
            fv = fold(value)
            matched = [t for t in terms if t in fv]
            if not matched:
                continue
            bits, df = rarity.discrete_bits(kind, value)
            hits.setdefault(iid, []).append({"kind": kind, "value": redact_text(value[:200]), "terms": matched,
                                             "bits": round(max(bits, 0.5), 2), "doc_freq": df})
    if len(genomes) <= HTML_FALLBACK_MAX_INCIDENTS:
        _html_fallback(terms, fold, genomes, hits, settings)
    ranked = []
    for iid, hs in hits.items():
        covered = {t for h in hs for t in h["terms"]}
        score = sum(sorted((h["bits"] for h in hs), reverse=True)[:5]) * len(covered) / len(terms)
        ranked.append({"incident_id": iid, "score": round(score, 2), "terms_covered": sorted(covered),
                       "matches": sorted(hs, key=lambda h: -h["bits"])[:8]})
    ranked.sort(key=lambda r: -r["score"])
    member_of = {m: c for c in campaigns for m in c.members}
    camp_scores: dict[str, dict[str, Any]] = {}
    for r in ranked:
        c = member_of.get(r["incident_id"])
        if c is None:
            continue
        cur = camp_scores.setdefault(c.id, {"campaign_id": c.id, "label": c.label, "score": 0.0, "incidents": []})
        cur["score"] = max(cur["score"], r["score"])
        cur["incidents"].append(r["incident_id"])
    try:  # Meta Ad Library (ENRICH-PLUS, B16): live with a token + HYDRA_ENRICH=1, else replay; never raises
        from hydra.enrich.adlibrary import hint_lookup

        adlib = hint_lookup(hint, settings, limit=limit)
    except Exception as exc:  # noqa: BLE001 — the ad library is optional; the corpus search still answers
        adlib = {"status": "error", "note": f"{type(exc).__name__}"[:120], "ads": []}
    return {"hint": hint, "base_run": base_run, "incidents": ranked[:limit],
            "campaigns": sorted(camp_scores.values(), key=lambda c: -c["score"])[:limit], "ad_library": adlib}


HTML_FALLBACK_MAX_INCIDENTS = 2000
HTML_FALLBACK_MAX_BYTES = 8 * 1024 * 1024


def _html_fallback(terms: list[str], fold, genomes: dict[str, IncidentGenome], hits: dict[str, list[dict[str, Any]]],
                   settings: Settings) -> None:
    """Hint search without TXT genes (or for words only in scripts / hidden DOM): scan the stored landing HTML
    of incidents that have no gene hit yet. Bytes are decoded and searched as text; nothing is rendered."""
    store = ContentStore(settings.cas_dir)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        rows = conn.execute(
            "SELECT ia.incident_id, ia.artifact_id, ia.path_in_incident FROM incident_artifacts ia JOIN artifacts a"
            " ON a.id = ia.artifact_id WHERE ia.role = 'landing_html' AND a.size <= ?",
            (HTML_FALLBACK_MAX_BYTES,)).fetchall()
    for r in rows:
        iid = r["incident_id"]
        if iid not in genomes or iid in hits or not store.exists(r["artifact_id"]):
            continue
        try:
            raw = store.read_bytes(r["artifact_id"])  # refuses symlinks / paths outside the CAS
        except OSError:
            continue
        text = fold(raw.decode("utf-8", errors="replace"))
        matched = [t for t in terms if t in text]
        if matched:
            hits.setdefault(iid, []).append({"kind": "page_html", "value": redact_text(r["path_in_incident"][:200]),
                                             "terms": matched, "bits": 1.0, "doc_freq": None})


def corpus_ready(settings: Settings) -> dict[str, Any]:
    """Pre-check for the API before it starts a single-item job (audit A10): is there a corpus to compare with?"""
    if not settings.db_path.exists():
        return {"ready": False, "run_id": None, "message_pl": ONBOARDING_PL}
    with closing(connect(settings.db_path, readonly=True)) as conn:
        run = latest_run(conn)
    return {"ready": run is not None, "run_id": run, "message_pl": None if run else ONBOARDING_PL}


ONBOARDING_PL = ("Nie ma jeszcze przeanalizowanego korpusu. Najpierw przeanalizuj zbiór (np. próbkę NASK: Analizuj"
                 " zbiór), aby nowe materiały miały z czym być porównane. Pojedynczy materiał zostanie przyjęty"
                 " i może zostać zatwierdzony jako pierwszy wzorzec.")


def detect_input_kind(item: str | Path) -> str:
    """`url` for a URL string, else `file` (files, incident folders and incident ZIPs)."""
    if isinstance(item, str) and re.match(r"^(https?://|www\.)", item.strip(), re.I):
        return "url"
    if isinstance(item, str) and not Path(item).expanduser().exists() and HOST_LIKE.match(item.strip()) \
            and not FILE_LIKE.search(item.strip()):
        return "url"  # 'pko-bonus24.com/oferta' without a scheme
    return "file"


FILE_LIKE = re.compile(r"\.(txt|json|jsonl|png|jpe?g|webp|gif|heic|pdf|zip|html?|mhtml?|har|mp4|mov|webm|csv|md|log)$",
                       re.I)
HOST_LIKE = re.compile(r"^[\w-]+(\.[\w-]+)*\.[a-z][\w-]{1,62}([/?#]\S*)?$", re.I)


def _refuse_inside_external_corpus(path: Path) -> None:
    """A folder inside a materialised external corpus must not be imported as organiser data (DATA-HUNT P1)."""
    from hydra.adapters.datasets import CORPUS_FILE

    for parent in list(path.parents)[:3]:
        if (parent / CORPUS_FILE).is_file():
            raise ValueError(f"{path} belongs to the external corpus {parent} ({CORPUS_FILE}); import the corpus"
                             " root (it carries source ext:<name> and labels), not a folder inside it")


def import_input(path: Path, writer: Writer, store: ContentStore, settings: Settings, *, source: str = "upload",
                 incident_id: str | None = None, role: Any | None = None,
                 errors: list[dict[str, str]] | None = None) -> Incident:
    """THE single-item dispatcher (audit A02): CLI `hydra assign`, GUI/API `analyse_input("file")`, the scheduler
    inbox and EVAL all import one new item through here, so the same bytes give the same incident. ZIPs are
    unpacked by the adapter's content-addressed `unpack_zip` (same bytes → same folder → same incident id); a ZIP
    or folder holding several incidents is a dataset and is refused here (use `analyse`)."""
    from hydra.adapters.nask_folder import classify_input

    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    _refuse_inside_external_corpus(path)
    kind = classify_input(path, settings)
    if kind == "dataset":
        raise ValueError(f"{path.name}: zbiór wielu incydentów — użyj analizy zbioru (hydra analyse / Analizuj"
                         " zbiór), nie analizy pojedynczego materiału")
    target = path
    if kind == "incident" and path.is_file():
        target = discover_incidents(path, settings)[0]
    rep = import_single(target, writer, store, settings, incident_id=incident_id, source=source, role=role)
    if errors is not None:  # per-file statuses for the input report (files skipped inside the folder/ZIP)
        errors.extend(rep.errors)
        if path.is_file() and kind == "incident":
            errors.extend(zip_skipped(path, settings))
    return rep.incident


def _already_imported(path: Path, settings: Settings) -> str | None:
    """Incident id when this single file (not a folder or archive) is already the artifact of its own
    content-addressed incident `new-<sha8>`; re-uploads under another name then reuse it (audit row "same bytes
    uploaded twice")."""
    from hydra.adapters.nask_folder import _is_archive
    from hydra.storage import sha256_file

    path = Path(path).expanduser()
    if not path.is_file() or _is_archive(path) or not settings.db_path.exists():
        return None
    digest, _ = sha256_file(path)
    iid = f"new-{digest[:8]}"
    with closing(connect(settings.db_path, readonly=True)) as conn:
        row = conn.execute("SELECT 1 FROM incident_artifacts WHERE incident_id = ? AND artifact_id = ?",
                           (iid, digest)).fetchone()
    return iid if row else None


FILE_STATUS_PL = {
    "ok": "odczytano cechy",
    "duplicate": "duplikat: te same bajty są już w korpusie",
    "unreadable": "plik uszkodzony lub nieczytelny: ekstraktory nie odczytały treści",
    "unsupported": "format nieobsługiwany: zapisano tylko skrót pliku (sha256)",
    "no_features": "brak cech: plik odczytany, ale bez treści do porównania",
    "skipped": "pominięty przy imporcie",
    "rejected": "odrzucony bez rozpakowania (zabezpieczenie archiwum)",
}
SKIP_CODE_PL = {
    "ZIP_PATH_TRAVERSAL": "ścieżka wychodzi poza archiwum (path traversal)", "ZIP_SYMLINK": "dowiązanie symboliczne",
    "FILE_TOO_LARGE": "plik przekracza limit rozmiaru", "FILE_EMPTY": "pusty plik (0 bajtów)",
    "HEIC_UNREADABLE": "obraz HEIC nieczytelny (brak dekodera: ffmpeg >= 7.1 lub sips)",
}


def input_report(iid: str, settings: Settings, errors: list[dict[str, str]], source: Path | None = None) -> dict:
    """Per-file status of one analysed input (robustness, NASK §6): every file of the incident is `ok`,
    `duplicate`, `unreadable`, `unsupported` or `no_features`; files dropped at import (traversal, symlink, too
    large, empty) are `skipped` with their code. Read only; nothing here changes the decision."""
    with closing(connect(settings.db_path, readonly=True)) as conn:
        arts = conn.execute(
            "SELECT ia.artifact_id, ia.role, ia.path_in_incident, a.mime, a.size FROM incident_artifacts ia"
            " JOIN artifacts a ON a.id = ia.artifact_id WHERE ia.incident_id = ? ORDER BY ia.path_in_incident",
            (iid,)).fetchall()
        genes = conn.execute("SELECT kind, status, json_extract(evidence, '$.artifact_id') AS aid FROM genes"
                             " WHERE incident_id = ?", (iid,)).fetchall()
        others: dict[str, str] = {}
        for a in arts:
            r = conn.execute("SELECT incident_id FROM incident_artifacts WHERE artifact_id = ? AND incident_id != ?"
                             " ORDER BY incident_id LIMIT 1", (a["artifact_id"], iid)).fetchone()
            if r:
                others[a["artifact_id"]] = r["incident_id"]
    files: list[dict[str, Any]] = []
    for a in arts:
        mine = [g for g in genes if g["aid"] == a["artifact_id"]]
        observed = [g for g in mine if g["status"] == GeneStatus.OBSERVED.value and g["kind"] != "artifact_sha256"]
        if a["artifact_id"] in others:
            status = "duplicate"
        elif observed:
            status = "ok"
        elif any(g["status"] == GeneStatus.FAILED.value for g in mine) or (
                a["mime"].startswith(("image/", "video/", "audio/")) and not a["mime"].startswith("image/svg")):
            status = "unreadable"
        elif a["role"] == "other":
            status = "unsupported"
        else:
            status = "no_features"
        f = {"path": redact_text(a["path_in_incident"]), "role": a["role"], "mime": a["mime"], "size": a["size"],
             "genes": len(observed), "status": status, "status_pl": FILE_STATUS_PL[status]}
        if status == "duplicate":
            f["duplicate_of"] = others[a["artifact_id"]]
            f["status_pl"] += f" (incydent {others[a['artifact_id']]})"
        files.append(f)
    for e in errors:
        code = str(e.get("code", ""))
        files.append({"path": redact_text(str(e.get("path", ""))), "status": "skipped", "code": code,
                      "status_pl": f"{FILE_STATUS_PL['skipped']}: {SKIP_CODE_PL.get(code, code)}"})
    counts: dict[str, int] = {}
    for f in files:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    if not files:
        summary = "Pusty materiał: brak plików do analizy" + (" (pusty folder)." if source and source.is_dir()
                                                                 else ".")
    else:
        summary = "Pliki: " + ", ".join(f"{FILE_STATUS_PL[k].split(':')[0]} {n}" for k, n in sorted(counts.items()))
    return {"files": files, "counts": counts, "summary_pl": summary}


def analyse_input(kind: str, payload: str | Path, settings: Settings, *, base_run: str | None = None,
                  only: list[str] | None = None, thresholds: Thresholds | None = None,
                  writer: Writer | None = None, incident_id: str | None = None) -> dict[str, Any]:
    """One entry point for the four input kinds of the brief (NASK_TASK.md, update 13:30):

    - `url`: isolated fetch (HYDRA_ENRICH=1 only; else replay or unavailable) → incident → genes → assign;
    - `screenshot`: image as `ad_creative` → genes (OCR url/domain genes) → follow the first URL as in `url`,
      added to the same incident → assign;
    - `hint`: keyword search over corpus genes → ranked incidents and campaigns (no new incident);
    - `file`: a file, incident folder or incident ZIP → normal import → assign.
    """

    from hydra.contracts import ArtifactRole

    if kind not in INPUT_KINDS:
        raise ValueError(f"kind must be one of {INPUT_KINDS}")
    if kind == "hint":
        return {"kind": kind, "search": search_hint(str(payload), settings, base_run=base_run)}
    th = thresholds or Thresholds()
    settings.ensure_dirs()
    own_writer = writer is None
    writer = writer or Writer(settings.db_path)
    store = ContentStore(settings.cas_dir)
    out: dict[str, Any] = {"kind": kind, "notes": []}
    from hydra.progress import INPUT_STAGES

    job_id = create_job(writer, "analyse_input", {"kind": kind, "payload": redact_text(str(payload))[:300]})
    progress = Progress(settings, writer, job_id, stages=INPUT_STAGES, input_kind=kind)
    out["job_id"] = job_id
    try:
        if kind == "url":
            url = str(payload).strip()
            if not re.match(r"^https?://", url, re.I):
                url = "https://" + url
            iid = f"new-{hashlib.sha256(url.encode()).hexdigest()[:8]}"
            folder, info = _fetch_into_folder(url, settings)
            out["fetch"] = info
            import_incident(folder, writer, store, settings, incident_id=iid, source="url")
        elif kind == "screenshot":
            rep = import_single(Path(payload), writer, store, settings, source="screenshot",
                                role=ArtifactRole.AD_CREATIVE)
            iid = rep.incident.id
            with closing(connect(settings.db_path, readonly=True)) as conn:
                inc = load_incident(conn, iid)
            assert inc is not None
            extract([inc], writer, store, settings, only=only)
            with closing(connect(settings.db_path, readonly=True)) as conn:
                found = [r["value"] for r in conn.execute(
                    "SELECT value FROM genes WHERE incident_id = ? AND status = 'observed' AND value IS NOT NULL"
                    f" AND kind IN ({','.join('?' * len(URL_GENE_KINDS))}) ORDER BY quality DESC",
                    (iid, *URL_GENE_KINDS))]
                phones = [r["value"] for r in conn.execute(
                    "SELECT DISTINCT value FROM genes WHERE incident_id = ? AND status = 'observed'"
                    " AND kind = 'entity_phone'", (iid,))]
            # a full URL (scheme + path) beats a bare domain read from the same address bar
            found = sorted(dict.fromkeys(found), key=lambda v: (not v.startswith(("http://", "https://")),
                                                                 "/" not in v.split("://", 1)[-1]))
            out["urls_in_image"] = found[:10]
            out["phones_in_image"] = phones[:10]
            if found:
                url = found[0] if found[0].startswith(("http://", "https://")) else f"https://{found[0]}"
                folder, info = _fetch_into_folder(url, settings)
                out["fetch"] = info
                out["notes"].append(f"Adres odczytany ze zrzutu (OCR): {redact_text(url)}. {info['status_pl']}")
                import_incident(folder, writer, store, settings, incident_id=iid, source="screenshot")
            else:
                out["notes"].append("Nie znaleziono adresu URL ani domeny na zrzucie ekranu.")
        else:
            import_errors: list[dict[str, str]] = []
            seen = _already_imported(Path(payload), settings) if incident_id is None else None
            try:
                if seen is not None:  # same bytes under any name: no second copy appended to the incident
                    iid = seen
                    out["duplicate_of"] = seen
                    out["notes"].append(f"Ten sam plik (te same bajty) był już analizowany jako incydent {seen};"
                                        " nie zaimportowano go ponownie, decyzja policzona na nowo.")
                else:
                    iid = import_input(Path(payload), writer, store, settings, source="upload",
                                       incident_id=incident_id, errors=import_errors).id
            except ZipRejected as exc:
                out["input_report"] = {"files": [{"path": redact_text(Path(payload).name), "status": "rejected",
                                                  "code": exc.code, "status_pl": FILE_STATUS_PL["rejected"]
                                                  + f" ({exc.code})"}],
                                       "summary_pl": f"Archiwum odrzucone bez rozpakowania: {exc.code}."}
                out.update({"incident_id": None, "run_id": None, "assignment": None})
                out["notes"].append(out["input_report"]["summary_pl"])
                progress.finish("done", decision=None)
                return out
            out["_import_errors"] = import_errors
        extra: dict[str, Any] = {}
        progress.set(incident_id=iid)
        # one `duplicate_of` field for every path (merge-batch3): the re-upload rule here and the batch rule of
        # hydra.batch both name the incident that already holds the same bytes; it is stored in Assignment.meta
        dup_meta = {"duplicate_of": out["duplicate_of"], "duplicate_reason": "same_bytes"} \
            if out.get("duplicate_of") else None
        run_id, a = _assign_incident(iid, settings, writer, store, base_run=base_run, only=only, th=th,
                                     item=str(payload), extra=extra, progress=progress, meta=dup_meta)
        out.update({"incident_id": iid, "run_id": run_id, "assignment": a.model_dump(),
                    "conformal": extra.get("conformal")})
        out["input_report"] = input_report(iid, settings, out.pop("_import_errors", None) or [],
                                           Path(payload) if kind == "file" else None)
        if kind == "url" and out.get("fetch"):
            out["notes"].append(out["fetch"]["status_pl"])
        return out
    except BaseException as exc:
        try:
            progress.finish("failed", error=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        if own_writer:
            writer.close()


def explain_assignment(key: str, settings: Settings) -> dict[str, Any] | None:
    """Everything the live /analyse view shows for one new item, by job id or incident id: progress (stages,
    per-extractor genes and seconds, candidates per index, pairs scored), the decision and `Assignment.meta`
    (top-3 candidates split by level and family, margin, bits missing to the link, robustness of the best link,
    conformal set). Reads only; cheap enough to poll."""
    from hydra.progress import run_progress

    prog = run_progress(key, settings)
    incident_id = (prog or {}).get("incident_id") or (None if prog else key)
    if not settings.db_path.exists() or incident_id is None:
        return {"progress": prog, "assignment": None} if prog else None
    with closing(connect(settings.db_path, readonly=True)) as conn:
        row = conn.execute("SELECT run_id, detail, created_at FROM assignments WHERE incident_id = ?"
                           " ORDER BY created_at DESC LIMIT 1", (incident_id,)).fetchone()
    if row is None:
        return {"incident_id": incident_id, "progress": prog, "assignment": None}
    detail = json.loads(row["detail"])
    a = assignment_from_detail(detail)
    return {"incident_id": incident_id, "run_id": row["run_id"], "progress": prog,
            "decision": a.decision, "campaign_id": a.campaign_id, "group_id": a.group_id, "reason_pl": a.reason_pl,
            "candidates": [{"campaign_id": c.campaign_id, "probability": c.probability, "support": c.support,
                            "total_bits": c.best_pair.total_bits if c.best_pair else None}
                           for c in a.candidates[:3]],
            "meta": a.meta, "assignment": a.model_dump()}


def all_pairs(ids: list[str]) -> list[tuple[str, str]]:
    return [(a, b) if a < b else (b, a) for a, b in combinations(sorted(ids), 2)]
