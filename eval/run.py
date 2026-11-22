"""HYDRA NEXUS evaluation harness (NASK task §6). Writes eval/results.json, eval/results.md and PNG charts, and
stores the metrics JSON in `runs.metrics` (kind='eval') through `hydra.db.Writer`.

    . scripts/env.sh && uv run python -m eval.run                 # full run (auto backend)
    uv run python -m eval.run --quick                            # sample + one synthetic set, fewer sweeps

Corpora (every result names its corpus):
- `sample`        : the 10 organiser incidents (REAL), labels from data/nask/labels/sample_labels.json
- `sample+syn`    : sample + synthetic set s2 (SYNTHETIC variants, public distractors, template clones)
- `syn-dev`       : synthetic set s1 only, used to choose thresholds (never used for the reported numbers)

Experiments: grouping (pairwise P/R/F1, B-cubed, false merges; campaign and campaign-group level),
leave-one-incident-out assignment, open-set rejection of held-out campaigns, hard negatives, duplicates,
incomplete data, formats, per-family ablation, baseline ladder, feature coverage, runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

HOME = Path.home() / "HydraNexus"
NASK = HOME / "data" / "nask"
EVAL_DIR = Path(__file__).resolve().parent
FAMILIES = ["VIS", "TXT", "INF", "ID", "NAR", "META"]
LADDER = ["B0_phash", "B1_text", "B2_naive_sum", "B3_rarity_bits", "LR_fusion"]


HYDRA_ROOT_OVERRIDE: str | None = None


def _prepend_hydra_root(argv: list[str]) -> None:
    """`--hydra-root DIR` evaluates another checkout's `hydra` package with this harness (used by eval-gate)."""
    if "--hydra-root" in argv:
        root = argv[argv.index("--hydra-root") + 1]
        sys.path.insert(0, str(Path(root).resolve()))
        globals()["HYDRA_ROOT_OVERRIDE"] = str(Path(root).resolve())
        for m in [m for m in sys.modules if m == "hydra" or m.startswith("hydra.")]:
            del sys.modules[m]


_prepend_hydra_root(sys.argv)

from eval import labels as L  # noqa: E402
from eval import metrics as M  # noqa: E402
from eval import scorers as S  # noqa: E402
from eval import variants as V  # noqa: E402
from eval.backend import (  # noqa: E402
    Backend,
    HydraBackend,
    IncidentRef,
    StubBackend,
    default_eval_state,
    make_backend,
)
from hydra.contracts import Gene  # noqa: E402


class Timer:
    def __init__(self) -> None:
        self.t: dict[str, float] = defaultdict(float)
        self.n: dict[str, int] = defaultdict(int)

    def add(self, stage: str, dt: float) -> None:
        self.t[stage] += dt
        self.n[stage] += 1

    def summary(self) -> dict:
        return {k: {"total_s": round(v, 3), "calls": self.n[k], "mean_s": round(v / max(1, self.n[k]), 4)}
                for k, v in sorted(self.t.items())}


T = Timer()
CRASHES: list[dict] = []
_T0 = time.perf_counter()


def log(msg: str) -> None:
    print(f"[eval {time.perf_counter() - _T0:7.1f}s] {msg}", file=sys.stderr, flush=True)


def guarded(what: str, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001 — every crash is counted and reported, never hidden
        CRASHES.append({"where": what, "error": f"{type(exc).__name__}: {exc}"[:300],
                        "trace": traceback.format_exc()[-800:]})
        return None


# --------------------------------------------------------------------------------------- corpus


def refs_from_rows(rows: list[dict]) -> list[IncidentRef]:
    return [IncidentRef(r["id"], Path(r["path"]), r.get("label"), r.get("group_label"), r.get("synthetic", False),
                        r) for r in rows]


def relabel(rows: list[dict], sample_rows: list[dict]) -> list[dict]:
    """Variants inherit the CURRENT labels of their parent (manifests may hold labels from an older label set)."""
    cur = {r["id"]: r for r in sample_rows}
    for r in rows:
        p = cur.get(r.get("parent") or "")
        if p and r.get("kind") == "variant":
            r["label"], r["group_label"] = p["label"], p["group_label"]
    return rows


def uniq_ids(rows: list[dict], tag: str) -> list[dict]:
    """Older manifests reused distractor ids (`syn-dis-000`) in every set: two different incidents under one id
    would be merged by CORE's import. Every id gets its set tag."""
    for r in rows:
        if not r["id"].endswith(tag) and f"-{tag}" not in r["id"]:
            r["id"] = f"{r['id']}-{tag}"
            if (r.get("label") or "").startswith("D-"):
                r["label"], r["group_label"] = f"D-{r['id']}", f"GD-{r['id']}"
    return rows


def relocate(rows: list[dict], variants_root: Path) -> list[dict]:
    """Manifests store absolute paths of the machine that generated them; re-root them under this machine's
    variants directory (laptop ↔ server)."""
    for r in rows:
        p = Path(r["path"])
        if not p.exists() and "/variants/" in r["path"]:
            r["path"] = str(variants_root / r["path"].split("/variants/", 1)[1])
    return rows


def parallel_extract(primary: HydraBackend, rows: list[dict], noenrich_rows: list[dict], tmp_root: Path,
                     workers: int) -> dict:
    """CORE extraction in `workers` subprocesses, each with its own state dir under tmp_root (stable shard
    assignment by id, so CORE's gene cache makes repeats cheap). The no-enrichment shards use an empty cache."""
    import subprocess
    import zlib

    tmp_root.mkdir(parents=True, exist_ok=True)
    empty = tmp_root / "empty-enrich-cache"
    empty.mkdir(exist_ok=True)
    seen, uniq = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    plan: list[tuple[Path, list[dict], Path | None]] = []
    for k in range(workers):
        part = [r for r in uniq if zlib.crc32(r["id"].encode()) % workers == k]
        if part:
            plan.append((tmp_root / f"shard-{k:02d}", part, None))
    n_ne = max(1, min(max(1, workers // 4), len(noenrich_rows)))
    for k in range(n_ne):
        part = [r for r in noenrich_rows if zlib.crc32(r["id"].encode()) % n_ne == k]
        if part:
            plan.append((tmp_root / f"noenrich-{k:02d}", part, empty))
    procs = []
    t0 = time.perf_counter()
    # shared-server budget: an explicit OMP_NUM_THREADS / HYDRA_OCR_WORKERS (CLAUDE.md) caps every shard
    fair = max(1, (os.cpu_count() or 2) // max(1, len(plan)))
    threads = str(min(fair, int(os.environ.get("OMP_NUM_THREADS") or fair)))
    ocr = os.environ.get("HYDRA_OCR_WORKERS") or threads
    env = {**os.environ, "OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads,
           "HYDRA_TORCH_THREADS": threads, "HYDRA_WORKERS": ocr, "TOKENIZERS_PARALLELISM": "false"}
    for state, part, cache in plan:
        state.mkdir(parents=True, exist_ok=True)
        (state / "rows.json").write_text(json.dumps(part))
        cmd = [sys.executable, "-W", "ignore", "-m", "eval.extract_shard", "--state", str(state), "--rows",
               str(state / "rows.json")] + (["--enrich-cache", str(cache)] if cache else []) + \
            (["--hydra-root", HYDRA_ROOT_OVERRIDE] if HYDRA_ROOT_OVERRIDE else [])
        log_f = (state / "shard.log").open("w")
        procs.append((state, part, subprocess.Popen(cmd, cwd=EVAL_DIR.parent, stdout=log_f, stderr=log_f,
                                                        env=env), log_f))
    for state, part, p, log_f in procs:
        p.wait()
        log_f.close()
        if p.returncode != 0:
            CRASHES.append({"where": f"extract-shard:{state.name}", "error": f"exit {p.returncode}"})
        for line in (state / "shard.log").read_text(errors="replace").splitlines():
            if line.startswith("SHARD-FAIL"):
                CRASHES.append({"where": f"extract:{line.split()[1].rstrip(':')}", "error": line[:300]})
        missing = primary.preload(state / "hydra.sqlite", [r["id"] for r in part])
        for m in missing:
            CRASHES.append({"where": f"extract:{m}", "error": "no genes extracted"})
    T.add("extract_parallel_wall", time.perf_counter() - t0)
    return {"shards": len(plan), "incidents": len(uniq) + len(noenrich_rows),
            "wall_s": round(time.perf_counter() - t0, 1)}


def extract_noenrich_serial(primary: HydraBackend, rows: list[dict], state_dir: Path) -> None:
    """Every sample incident re-extracted with an EMPTY enrichment cache in a separate eval state."""
    empty = Path(tempfile.mkdtemp(prefix="hydra-eval-noenrich-"))
    other = HydraBackend(state_dir=state_dir / "noenrich", enrich_cache=empty)
    try:
        for r in rows:
            g = guarded(f"noenrich-extract:{r['parent']}", other.extract,
                        IncidentRef(r["parent"], Path(r["path"]), r["label"], r["group_label"]))
            if g is not None:
                primary.adopt(other, r["parent"], r["id"])
                primary._preloaded.add(r["id"])
    finally:
        other.close()


class Corpus:
    def __init__(self, name: str, refs: list[IncidentRef], backend: Backend) -> None:
        self.backend_name = backend.name
        self.name = name
        self.refs = {r.iid: r for r in refs}
        self.genes: dict[str, list[Gene]] = {}
        for r in refs:
            t0 = time.perf_counter()
            g = guarded(f"extract:{r.iid}", backend.extract, r)
            T.add("extract_incident", time.perf_counter() - t0)
            self.genes[r.iid] = g if g is not None else []
        self.disputed: set = set()
        self.labels = {i: r.label for i, r in self.refs.items() if r.label}
        self.groups = {i: r.group_label for i, r in self.refs.items() if r.group_label}

    def relabelled(self, overrides: dict[str, dict], suffix: str = "") -> Corpus:
        """Same genes, labels changed for the listed source incidents (and every variant derived from them)."""
        c = Corpus.__new__(Corpus)
        c.name, c.refs, c.genes = self.name + suffix, self.refs, self.genes
        c.labels, c.groups = dict(self.labels), dict(self.groups)
        c.disputed = getattr(self, "disputed", set())
        for iid, r in self.refs.items():
            src = (r.meta or {}).get("parent") or iid
            o = overrides.get(src)
            if o:
                if "label" in o:
                    c.labels[iid] = o["label"]
                if "group_label" in o:
                    c.groups[iid] = o["group_label"]
        return c

    def subset(self, ids) -> dict[str, list[Gene]]:
        return {i: self.genes[i] for i in ids}


_BUILDS: dict = {}
CTX: dict = {"backend": None, "corpora": {}, "backends": {}}
POOL = None


MECH: dict = {"active": None}
UNIFORM_BITS = 4.0  # "no rarity weighting": every non-stop shared DISCRETE value is worth the same. Vector
# background (embedding surprise) and near-hash bits are not changed by this ablation.


def _override_sig(backend) -> tuple:
    th = getattr(backend, "th", None)
    if th is None:
        return ()
    import hydra.pipeline as PL
    import hydra.synapse as SY

    return (th.link, th.kit, SY.FAMILY_CAP, SY.PLATFORM_KIT_CAP, getattr(PL.retrieve, "eval_all_pairs_below", None),
            getattr(backend, "abstain_min", None), MECH["active"])


def _apply_mechanism(name: str) -> list:
    """Mechanism ablations by monkeypatching CORE inside this (worker) process. Returns undo actions."""
    import functools

    import hydra.entropy as EN
    import hydra.pipeline as PL
    import hydra.synapse as SY

    undo = []

    def setattr_(obj, attr, val):
        undo.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, val)

    if name == "no_rarity":
        orig = EN.Rarity.discrete_bits

        def uniform(self, *args, **kwargs):  # full passthrough: CORE adds keywords (operator=..., kit=...)
            b, df = orig(self, *args, **kwargs)
            return (UNIFORM_BITS if b > 0 else 0.0), df

        setattr_(EN.Rarity, "discrete_bits", uniform)
    elif name == "no_tiers":
        setattr_(SY, "gene_tier", lambda g: None)
        setattr_(SY, "is_kit", lambda g: False)
    elif name == "no_caps":
        setattr_(SY, "GROUP_CAP", 1e9)
        setattr_(SY, "FAMILY_CAP", 1e9)
        setattr_(SY, "REST_WEIGHT", 1.0)
    elif name == "no_guards":
        setattr_(PL, "cluster", functools.partial(PL.cluster, max_size=10**9, max_group_campaigns=10**9,
                                                  max_group_members=10**9))
    else:
        raise ValueError(f"unknown mechanism ablation {name}")
    MECH["active"] = name
    return undo


class _Overrides:
    """Temporarily set CORE thresholds / caps (link, kit, FAMILY_CAP, PLATFORM_KIT_CAP), the assignment abstain
    floor, candidate filtering and mechanism ablations on the context backend."""

    def __init__(self, backend, ov: dict | None) -> None:
        self.b, self.ov, self.saved, self.undo = backend, ov or {}, None, []

    def __enter__(self):
        if not self.ov or getattr(self.b, "th", None) is None:
            return self
        import hydra.pipeline as PL
        import hydra.synapse as SY

        self.saved = (self.b.th.link, self.b.th.kit, SY.FAMILY_CAP, SY.PLATFORM_KIT_CAP, PL.retrieve,
                      getattr(self.b, "abstain_min", None))
        if "all_pairs_below" in self.ov:  # retrieval test: force or keep CORE's candidate filtering
            import functools

            from hydra.retrieval import candidates

            f = functools.partial(candidates, all_pairs_below=self.ov["all_pairs_below"])
            f.eval_all_pairs_below = self.ov["all_pairs_below"]
            PL.retrieve = f
        self.b.th.link = self.ov.get("link", self.b.th.link)
        self.b.th.kit = self.ov.get("kit", self.b.th.kit)
        SY.FAMILY_CAP = self.ov.get("FAMILY_CAP", SY.FAMILY_CAP)
        SY.PLATFORM_KIT_CAP = self.ov.get("PLATFORM_KIT_CAP", SY.PLATFORM_KIT_CAP)
        if "abstain_min" in self.ov:
            self.b.abstain_min = self.ov["abstain_min"]
        if self.ov.get("mech"):
            self.undo = _apply_mechanism(self.ov["mech"])
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            import hydra.pipeline as PL
            import hydra.synapse as SY

            (self.b.th.link, self.b.th.kit, SY.FAMILY_CAP, SY.PLATFORM_KIT_CAP, PL.retrieve,
             abst) = self.saved
            self.b.abstain_min = abst
        for obj, attr, val in reversed(self.undo):
            setattr(obj, attr, val)
        if self.undo:
            MECH["active"] = None
        return False


def _worker_init() -> None:
    """One thread per fold worker (torch + BLAS), otherwise 64 workers x 64 threads oversubscribe the server."""
    # pool workers are daemonic: CORE's own scoring fork_map (hydra/parallel.py) must stay in-process here
    os.environ["HYDRA_SCORE_WORKERS"] = "0"
    try:
        import torch

        torch.set_num_threads(1)
    except Exception:  # noqa: BLE001
        pass
    try:
        from threadpoolctl import threadpool_limits

        threadpool_limits(1)
    except Exception:  # noqa: BLE001
        pass


def start_pool(workers: int) -> None:
    """Fork a process pool AFTER every corpus is loaded and BEFORE the parent runs any torch code (libgomp is
    not fork-safe once its thread pool exists). Workers inherit CTX copy-on-write; jobs are independent."""
    global POOL
    import multiprocessing as mp

    POOL = mp.get_context("fork").Pool(workers, initializer=_worker_init)


def stop_pool() -> None:
    global POOL
    if POOL is not None:
        POOL.close()
        POOL.join()
        POOL = None


def pmap(fn, jobs: list) -> list:
    if POOL is None or len(jobs) < 2:
        return [fn(j) for j in jobs]
    return POOL.map(fn, jobs, chunksize=1)


def _fold(job: dict) -> dict:
    """One independent unit: build on `build_ids` of a registered corpus (rarity rebuilt on exactly those
    incidents), then assign each of `assign_ids`. Returns judged rows, crashes and stage timings."""
    T.t.clear()
    T.n.clear()
    crashes_before = len(CRASHES)
    backend = CTX["backends"].get(job.get("backend")) or CTX["backend"]
    corpus = CTX["corpora"][job["corpus"]]
    drop = frozenset(job.get("drop", ()))
    rows: list[dict] = []
    metrics = None
    with _Overrides(backend, job.get("ov")):
        conformal_saved = getattr(backend, "conformal", None)
        if job.get("retrieval"):
            import threading

            import psutil

            _BUILDS.clear()
            proc, peak_rss, stop = psutil.Process(), [0], threading.Event()
            base_rss = proc.memory_info().rss

            def _sample() -> None:  # RSS sampler (tracemalloc slowed scoring several-fold)
                while not stop.is_set():
                    peak_rss[0] = max(peak_rss[0], proc.memory_info().rss)
                    stop.wait(0.2)

            sampler = threading.Thread(target=_sample, daemon=True)
            sampler.start()
            if conformal_saved is not None:  # time candidates + scoring + clustering, not the calibration
                backend.conformal = False
            t0 = time.perf_counter()
        b = guarded(f"{job.get('tag', 'fold')}-build", build_timed, backend, corpus.subset(job["build_ids"]),
                    corpus.labels, drop, job.get("stage", "build_fold"))
        if job.get("retrieval"):
            wall = time.perf_counter() - t0
            stop.set()
            sampler.join()
            peak = max(0, peak_rss[0] - base_rss)
            if conformal_saved is not None:
                backend.conformal = conformal_saved
        if b is not None and job.get("retrieval"):
            ids = job["build_ids"]
            lab = {i: corpus.labels[i] for i in ids if i in corpus.labels}
            pos = {(x, y) for x, y in combinations(sorted(lab), 2) if lab[x] == lab[y]}
            cand = set(b.pairs)
            g = grouping_metrics(b, corpus)
            metrics = {"n_incidents": len(ids), "candidate_pairs": len(cand),
                       "all_pairs": len(ids) * (len(ids) - 1) // 2, "true_pairs": len(pos),
                       "candidate_recall": round(len(pos & cand) / len(pos), 4) if pos else None,
                       "connectivity": M.connectivity_recall(cand, lab),
                       "campaign_f1": g["campaign"]["pairwise"]["f1"], "false_merges": g["campaign"]["pairwise"]["fp"],
                       "group_f1": g["group"]["pairwise"]["f1"], "group_false_merges": g["group"]["pairwise"]["fp"],
                       "build_wall_s": round(wall, 2), "peak_rss_increase_mb": round(peak / 2**20, 1)}
        elif b is not None:
            if job.get("metrics"):
                g = grouping_metrics(b, corpus)
                metrics = {"campaign_f1": g["campaign"]["pairwise"]["f1"],
                           "campaign_bcubed_f1": g["campaign"]["bcubed"]["f1"],
                           "false_merges": g["campaign"]["pairwise"]["fp"],
                           "group_f1": g["group"]["pairwise"]["f1"],
                           "group_false_merges": g["group"]["pairwise"]["fp"],
                           "n_campaigns": len(set(b.member_of.values())),
                           "member_of": ({i: b.member_of.get(i) for i in job["build_ids"]} if job.get("all_members")
                                         else {i: b.member_of.get(i) for i in job.get("report_members", [])})}
            for iid in job.get("assign_ids", []):
                a = guarded(f"{job.get('tag', 'fold')}-assign:{iid}", assign_timed, backend, b, iid,
                            corpus.genes[iid], drop)
                if a is not None:
                    r = judge(a, b, corpus, iid, job["build_ids"])
                    r["decided_by"] = (a.meta or {}).get("eval_conformal", {}).get("decided_by", "threshold")
                    r.update(job.get("row_extra", {}).get(iid, {}))
                    rows.append(r)
    return {"rows": rows, "metrics": metrics, "crashes": CRASHES[crashes_before:],
            "timer": {k: (T.t[k], T.n[k]) for k in T.t}}


def _merge(results: list[dict]) -> list[dict]:
    """Fold results → crashes and timings into the parent's accumulators; returns the list unchanged."""
    if POOL is None:
        return results
    for r in results:
        CRASHES.extend(r["crashes"])
        for k, (t, n) in r["timer"].items():
            T.t[k] += t
            T.n[k] += n
    return results


def run_folds(jobs: list[dict], backend: Backend | None = None) -> list[dict]:
    reg = [k for k, v in CTX["backends"].items() if v is backend] if backend is not None else []
    if reg and backend is not CTX["backend"]:
        jobs = [{**j, "backend": reg[0]} for j in jobs]
        backend = None
    if backend is not None and backend is not CTX["backend"]:  # e.g. a ladder scorer: serial, swapped context
        saved = CTX["backend"]
        CTX["backend"] = backend
        try:
            pool, globals()["POOL"] = POOL, None
            return run_folds(jobs)
        finally:
            globals()["POOL"] = pool
            CTX["backend"] = saved
    if POOL is None:  # serial: keep the parent's timers/crashes (they accumulate directly)
        out = []
        for j in jobs:
            saved_t, saved_n = dict(T.t), dict(T.n)
            r = _fold(j)
            for k, v in saved_t.items():  # _fold resets timers; restore the running totals
                T.t[k] += v
            for k, v in saved_n.items():
                T.n[k] += v
            r["crashes"] = []
            out.append(r)
        return out
    return _merge(pmap(_fold, jobs))


def build_timed(backend: Backend, genes, labels, drop=frozenset(), stage="build"):
    """Builds are memoised per (backend, incident set, dropped families): LOO, robustness and open-set reuse
    the same rebuilds. Only cached while the gene lists are the same objects."""
    key = (id(backend), tuple(sorted(genes)), tuple(id(genes[i]) for i in sorted(genes)), frozenset(drop),
           _override_sig(backend))
    if key in _BUILDS:
        return _BUILDS[key]
    t0 = time.perf_counter()
    b = backend.build(genes, labels, drop)
    T.add(stage, time.perf_counter() - t0)
    _BUILDS[key] = b
    return b


def assign_timed(backend: Backend, b, iid, genes, drop=frozenset()):
    t0 = time.perf_counter()
    a = backend.assign(b, iid, genes, drop)
    T.add("assign", time.perf_counter() - t0)
    return a


# --------------------------------------------------------------------------------------- experiments


def grouping_metrics(b: S.Build, corpus: Corpus) -> dict:
    out = {"campaign": M.grouping(b.member_of, corpus.labels), "group": M.grouping(b.group_of, corpus.groups)}
    disputed = getattr(corpus, "disputed", set())
    if disputed:
        out["campaign"]["pairwise_confirmed_only"] = M.pairwise(b.member_of, corpus.labels, ignore=disputed)
        out["campaign"]["disputed_pairs"] = {
            "n": sum(1 for x, y in disputed if x in b.member_of and y in b.member_of),
            "linked_by_system": sum(1 for x, y in disputed if x in b.member_of and y in b.member_of
                                    and b.member_of[x] == b.member_of[y])}
    return out


def _campaign_maps(b: S.Build, corpus: Corpus):
    camp_label = M.campaign_majority(b.member_of, corpus.labels)
    group_major = M.campaign_majority(b.group_of, corpus.groups)
    camp_group = {c.id: getattr(c, "group_id", None) or b.group_of.get(c.members[0]) for c in b.campaigns}
    return camp_label, group_major, camp_group


def judge(a, b: S.Build, corpus: Corpus, iid: str, rest: list[str]) -> dict:
    lab, grp = corpus.labels.get(iid), corpus.groups.get(iid)
    camp_label, group_major, camp_group = _campaign_maps(b, corpus)
    known = lab is not None and any(corpus.labels.get(j) == lab for j in rest)
    assigned = camp_label.get(a.campaign_id) if a.campaign_id else None
    row = {"incident": iid, "decision": str(a.decision), "campaign_id": a.campaign_id, "assigned_label": assigned,
           "true_label": lab, "label_known": known,
           "outcome": M.assignment_outcome(str(a.decision), assigned, lab or "", known),
           "top_score": a.candidates[0].best_pair.total_bits if a.candidates and a.candidates[0].best_pair else None,
           "synthetic": corpus.refs[iid].synthetic, "kind": (corpus.refs[iid].meta or {}).get("kind"),
           "cand_labels": [camp_label.get(c.campaign_id) for c in a.candidates],
           "conformal_set_labels": [camp_label.get(c) for c in
                                    ((a.meta or {}).get("eval_conformal") or {}).get("prediction_set") or []]}
    if grp is not None:
        gknown = any(corpus.groups.get(j) == grp for j in rest)
        gid = getattr(a, "group_id", None) or (camp_group.get(a.campaign_id) if a.campaign_id else None)
        if gid is None and str(a.decision) == "new_campaign":
            # A new campaign kit-linked to a campaign that has no group yet (its siblings were held out): CORE
            # reports this as a shared_template candidate, which is group-level membership.
            for cand in a.candidates:
                if cand.best_pair is not None and str(cand.best_pair.hypothesis) == "shared_template":
                    gid = camp_group.get(cand.campaign_id)
                    row["group_via_kit_candidate"] = True
                    break
        gassigned = group_major.get(gid) if gid else None
        gdec = "assign" if gid else "new_campaign"
        if str(a.decision) == "abstain" and not gid:
            gdec = "abstain"
        row["group_outcome"] = M.assignment_outcome(gdec, gassigned, grp, gknown)
        row["group_known"] = gknown
    return row


def _summ(rows: list[dict]) -> dict:
    out = M.summarise_outcomes(rows)
    grows = [{"outcome": r["group_outcome"], "label_known": r["group_known"]} for r in rows if "group_outcome" in r]
    out["group_level"] = M.summarise_outcomes(grows) if grows else None
    return out


def loo_jobs(corpus: Corpus, drop=frozenset(), ov: dict | None = None, ids: list[str] | None = None) -> list[dict]:
    ids = ids or sorted(i for i in corpus.genes if i in corpus.labels)
    all_ids = sorted(corpus.genes)
    return [{"corpus": corpus.name, "build_ids": [j for j in all_ids if j != iid], "assign_ids": [iid],
             "drop": sorted(drop), "ov": ov, "tag": "loo", "stage": "build_loo"} for iid in ids]


def leave_one_out(backend: Backend, corpus: Corpus, ids: list[str] | None = None, drop=frozenset(),
                  ov: dict | None = None) -> dict:
    register(corpus)
    rows = [r for f in run_folds(loo_jobs(corpus, drop, ov, ids), backend) for r in f["rows"]]
    return {"summary": _summ(rows), "rows": rows}


CAMPAIGN_LABELS: set[str] = set()  # labels of the organiser sample (set in main from the labels file)


def is_campaign_label(lab: str) -> bool:
    """A held-out campaign must come from the sample's label set (C-* now, R-* in the label-protocol export)."""
    return lab in CAMPAIGN_LABELS or lab.startswith(("C-", "R-"))


def openset_jobs(corpus: Corpus, ov: dict | None = None) -> list[dict]:
    by_label = defaultdict(list)
    for i, lab in corpus.labels.items():
        by_label[lab].append(i)
    jobs = []
    for lab, members in sorted(by_label.items()):
        if not is_campaign_label(lab):
            continue
        jobs.append({"corpus": corpus.name, "build_ids": sorted(set(corpus.genes) - set(members)),
                     "assign_ids": sorted(members), "ov": ov, "tag": f"openset:{lab}", "stage": "build_openset",
                     "row_extra": {m: {"held_out_label": lab} for m in members}})
    return jobs


def open_set(backend: Backend, corpus: Corpus, ov: dict | None = None) -> dict:
    """Hold out every member of one campaign label (sample-derived labels only), rebuild, assign each back:
    the correct answer is new_campaign at campaign level."""
    register(corpus)
    rows = [r for f in run_folds(openset_jobs(corpus, ov), backend) for r in f["rows"]]
    s = M.summarise_outcomes(rows)
    sizes = Counter(r["held_out_label"] for r in rows)
    grp = [r for r in rows if "group_outcome" in r]
    sibling = sum(1 for r in rows if r["outcome"] == "false_assign" and r.get("group_outcome") == "correct_assign")
    return {"summary": {"n": s["n"], "rejection_rate": s["novel_rejection"], "false_assign": s["false_assign"],
                        "rejection_ci95": s["novel_rejection_ci95"],
                        "abstain": s["abstain"], "campaigns_held_out": len({r["held_out_label"] for r in rows}),
                        "false_assign_to_sibling_in_same_group": sibling,
                        "composition": {"multi_member_campaigns": sum(1 for v in sizes.values() if v > 1),
                                        "members_of_multi_member": sum(v for v in sizes.values() if v > 1),
                                        "singleton_campaigns": sum(1 for v in sizes.values() if v == 1)},
                        "group_level_correct": round(sum(r["group_outcome"] in ("correct_assign", "correct_new")
                                                         for r in grp) / len(grp), 4) if grp else None},
            "rows": rows}


def register(corpus: Corpus) -> None:
    """Make a corpus visible to fold jobs. With a pool, every corpus must be registered before start_pool."""
    known = CTX["corpora"].get(corpus.name)
    if known is not None and known is not corpus and POOL is not None:
        if known.labels != corpus.labels or set(known.genes) != set(corpus.genes):
            raise RuntimeError(f"corpus {corpus.name} changed after the pool was forked")
        return
    CTX["corpora"][corpus.name] = corpus


def hard_negatives(b: S.Build, labels_meta: dict) -> dict:
    rows = []
    for hn in labels_meta.get("hard_negatives", []):
        for x in hn["a"]:
            for y in hn["b"]:
                if x in b.member_of and y in b.member_of:
                    k = tuple(sorted([x, y]))
                    ps = b.pairs.get(k)
                    rows.append({"a": x, "b": y, "same_campaign": b.member_of[x] == b.member_of[y],
                                 "same_group": b.group_of.get(x) == b.group_of.get(y),
                                 "score": ps.total_bits if ps else None, "why": hn["why"]})
    return {"pairs": len(rows), "violations_campaign": sum(r["same_campaign"] for r in rows),
            "violations_group": sum(r["same_group"] for r in rows), "rows": rows}


def robustness_set(sample_rows: list[dict], out: Path, seed: int = 7) -> dict[str, list[dict]]:
    """Duplicates, incomplete data and format variants of each sample incident (SYNTHETIC)."""
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    cases = {
        "dup_exact": (["exact_dup"], False), "dup_near": (["near_dup"], False),
        "drop_har": (["drop_har"], False), "drop_landing": (["drop_landing"], False),
        "drop_creatives": (["drop_creatives"], False), "only_har": (["drop_landing", "drop_creatives"], False),
        "fmt_zip": ([], True), "fmt_mhtml": (["mhtml"], False), "fmt_rename_ext": (["rename_ext"], False),
        "fmt_har_dotname": (["har_dotname"], False), "fmt_nested_bundle": (["nest_bundle"], False),
        "noise_misfiled_har": ([], False),
    }
    hars = [p for r in sample_rows for p in Path(r["path"]).rglob("*.har")]
    res: dict[str, list[dict]] = {}
    for case, (muts, as_zip) in cases.items():
        rows = []
        for r in sample_rows:
            vid = f"rob-{case}-{r['id']}"
            extra = None
            if case == "noise_misfiled_har":
                own = set(Path(r["path"]).rglob("*.har"))
                others = [h for h in hars if h not in own]
                extra = rng.choice(others) if others else None
            try:
                path, log = V.make_variant(Path(r["path"]), out / vid, muts, rng, extra_har=extra, as_zip=as_zip)
            except Exception as exc:  # noqa: BLE001
                CRASHES.append({"where": f"variant:{vid}", "error": str(exc)[:300]})
                continue
            rows.append({"id": vid, "path": str(path), "label": r["label"], "group_label": r["group_label"],
                         "parent": r["id"], "kind": case, "synthetic": True, "mutations": log})
        res[case] = rows
    return res


def gene_stability(orig: list[Gene], var: list[Gene]) -> float:
    def key(gs):
        return {(g.kind, g.value) for g in gs if str(g.status) == "observed" and g.kind not in {"asset_sha256"}}
    a, b = key(orig), key(var)
    return round(len(a & b) / len(a | b), 4) if a | b else 1.0


def robust_corpus(base: Corpus, rob_corpus: Corpus) -> Corpus:
    """Base corpus + every robustness variant (genes, refs, labels) under one name for fold jobs."""
    c = Corpus.__new__(Corpus)
    c.name = f"{base.name}+robust"
    c.refs = {**base.refs, **rob_corpus.refs}
    c.genes = {**base.genes, **rob_corpus.genes}
    c.labels = {**base.labels, **rob_corpus.labels}
    c.groups = {**base.groups, **rob_corpus.groups}
    c.disputed = getattr(base, "disputed", set())
    return c


def robustness(backend: Backend, base: Corpus, rc: Corpus, loo_rows: dict[str, dict]) -> dict:
    """Duplicates: added to the full corpus (must link to the parent, no extra campaigns). Every other case:
    the variant is assigned into a build WITHOUT its parent (one fold per parent serves all its variants)."""
    register(rc)
    by_case: dict[str, list[IncidentRef]] = defaultdict(list)
    for iid, r in rc.refs.items():
        if iid not in base.refs:
            by_case[(r.meta or {}).get("kind", "?")].append(r)
    out = {}
    base_ids = sorted(base.genes)
    jobs = [{"corpus": rc.name, "build_ids": base_ids, "metrics": True, "tag": "dup-base"}]
    for case in sorted(c for c in by_case if c.startswith("dup_")):
        ids = sorted(r.iid for r in by_case[case])
        parents = sorted({by_case_ref.meta["parent"] for by_case_ref in by_case[case]})
        jobs.append({"corpus": rc.name, "build_ids": sorted(base_ids + ids), "metrics": True, "tag": f"dup:{case}",
                     "report_members": ids + parents, "case": case})
    by_parent: dict[str, list[str]] = defaultdict(list)
    for case, refs in by_case.items():
        if not case.startswith("dup_"):
            for r in refs:
                by_parent[r.meta["parent"]].append(r.iid)
    for parent, ids in sorted(by_parent.items()):
        jobs.append({"corpus": rc.name, "build_ids": [j for j in base_ids if j != parent], "assign_ids": sorted(ids),
                     "tag": f"robust:{parent}", "stage": "build_robust"})
    results = run_folds(jobs)
    n_camp = results[0]["metrics"]["n_campaigns"] if results[0]["metrics"] else None
    rows_by_case: dict[str, list[dict]] = defaultdict(list)
    for job, r in zip(jobs, results, strict=True):
        if job["tag"].startswith("dup:") and r["metrics"]:
            mo = r["metrics"]["member_of"]
            ids = [i for i in job["report_members"] if i.startswith("rob-")]
            linked = [mo.get(i) == mo.get(rc.refs[i].meta["parent"]) for i in ids]
            out[job["case"]] = {"n": len(ids), "linked_to_parent_rate": round(sum(linked) / max(1, len(linked)), 4),
                                "campaigns_without_dups": n_camp, "campaigns_with_dups": r["metrics"]["n_campaigns"],
                                "campaign_inflation": (r["metrics"]["n_campaigns"] - n_camp) if n_camp else None,
                                "crashes": len(r["crashes"])}
        for row in r["rows"]:
            ref = rc.refs[row["incident"]]
            parent = ref.meta["parent"]
            row["parent"] = parent
            row["parent_outcome"] = loo_rows.get(parent, {}).get("outcome")
            row["agrees_with_parent"] = row["outcome"] == row["parent_outcome"]
            row["gene_stability"] = gene_stability(base.genes.get(parent, []), rc.genes.get(row["incident"], []))
            rows_by_case[ref.meta.get("kind", "?")].append(row)
    for case, rows in sorted(rows_by_case.items()):
        s = M.summarise_outcomes(rows)
        out[case] = {"n": len(rows), "accuracy": s["accuracy"],
                     "agreement_with_undegraded": round(sum(x["agrees_with_parent"] for x in rows) / max(1, len(rows)),
                                                        4),
                     "mean_gene_stability": round(sum(x["gene_stability"] for x in rows) / max(1, len(rows)), 4),
                     "outcomes": {k: s[k] for k in M.OUTCOMES}, "crashes": 0}
    return out


def ablation(backend: Backend, corpus: Corpus, full_loo: dict, full_group: dict) -> dict:
    register(corpus)
    out = {"full": {"loo_accuracy": full_loo["summary"]["accuracy"],
                    "campaign_f1": full_group["campaign"]["pairwise"]["f1"],
                    "group_f1": full_group["group"]["pairwise"]["f1"],
                    "false_merges": full_group["campaign"]["pairwise"]["fp"]}}
    jobs, owner = [], []
    variants = [(f"-{f}", frozenset({f})) for f in FAMILIES] + \
        [(f"only-{f}", frozenset(FAMILIES) - {f}) for f in FAMILIES]
    for name, drop in variants:
        jobs.append({"corpus": corpus.name, "build_ids": sorted(corpus.genes), "drop": sorted(drop), "metrics": True,
                     "tag": f"ablation:{name}", "stage": "build_ablation"})
        owner.append((name, "full"))
        for j in loo_jobs(corpus, drop):
            jobs.append(j)
            owner.append((name, "loo"))
    results = run_folds(jobs)
    for fam, _drop in variants:
        full = [r for (f, k), r in zip(owner, results, strict=True) if f == fam and k == "full"][0]["metrics"]
        rows = [row for (f, k), r in zip(owner, results, strict=True) if f == fam and k == "loo" for row in r["rows"]]
        if full is None:
            continue
        acc = M.summarise_outcomes(rows)["accuracy"]
        r = {"loo_accuracy": acc, "campaign_f1": full["campaign_f1"], "group_f1": full["group_f1"],
             "false_merges": full["false_merges"]}
        r["delta_loo_accuracy"] = round((out["full"]["loo_accuracy"] or 0) - (acc or 0), 4)
        r["delta_campaign_f1"] = round(out["full"]["campaign_f1"] - r["campaign_f1"], 4)
        r["delta_group_f1"] = round(out["full"]["group_f1"] - r["group_f1"], 4)
        out[fam] = r
    return out


def coverage(corpus: Corpus) -> dict:
    n = len(corpus.genes)
    kinds: Counter = Counter()
    fams: Counter = Counter()
    for gs in corpus.genes.values():
        ks = {(str(g.family), g.kind) for g in gs if str(g.status) == "observed"}
        for f, k in ks:
            kinds[f"{f}:{k}"] += 1
        for f in {f for f, _ in ks}:
            fams[f] += 1
    return {"n_incidents": n, "family": {f: round(fams[f] / n, 3) for f in FAMILIES},
            "kind": {k: round(v / n, 3) for k, v in sorted(kinds.items())}}


def tune(scorer_name: str, dev: Corpus) -> dict:
    """Pick the campaign threshold on the synthetic dev set (max pairwise F1; ties → higher threshold) and the
    group threshold (max group-level pairwise F1, ≤ campaign threshold)."""
    cls = S.SCORERS[scorer_name]
    grid = cls.grid
    best = (-1.0, None)
    curve = []
    for t in grid:
        bk = StubBackend(scorer_name, threshold=t, group_threshold=t)
        b = bk.build(dev.genes, dev.labels)
        f1 = M.pairwise(b.member_of, dev.labels)["f1"]
        curve.append({"threshold": t, "campaign_f1": f1})
        if f1 >= best[0]:
            best = (f1, t)
    gbest = (-1.0, best[1])
    for t in [g for g in grid if g <= best[1]]:
        bk = StubBackend(scorer_name, threshold=best[1], group_threshold=t)
        b = bk.build(dev.genes, dev.labels)
        f1 = M.pairwise(b.group_of, dev.groups)["f1"]
        if f1 > gbest[0]:
            gbest = (f1, t)
    return {"threshold": best[1], "dev_campaign_f1": best[0], "group_threshold": gbest[1],
            "dev_group_f1": gbest[0], "curve": curve}


LINK_GRID = [6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 20.0, 24.0]
SWEEP_ASSIGN = False  # LOO + open set along the link axis (--sweep-assign): ~500 folds, flat on earlier runs
KIT_GRID = [3.0, 4.0, 6.0, 8.0]
FAMILY_CAP_GRID = [12.0, 16.0, 24.0, 32.0]
PLATFORM_KIT_CAP_GRID = [0.0, 2.0, 4.0]


def core_sweep(bk: HydraBackend, corpora: list[Corpus], quick: bool) -> dict:
    """Sensitivity of CORE to its thresholds (link, kit) and caps (FAMILY_CAP, PLATFORM_KIT_CAP).
    Grouping metrics for the whole grid on every corpus; LOO + open-set along the link axis (kit at CORE's
    default) on the last corpus. Nothing here changes what the main results use (CORE defaults)."""
    import hydra.synapse as SY

    for c in corpora:
        register(c)
    d = {"link": bk.th.link, "kit": bk.th.kit, "FAMILY_CAP": SY.FAMILY_CAP, "PLATFORM_KIT_CAP": SY.PLATFORM_KIT_CAP}
    out: dict = {"defaults": d, "grid": [], "link_axis": [], "caps": []}
    jobs, meta = [], []
    for c in corpora:
        for link in LINK_GRID:
            for kit in KIT_GRID:
                if kit <= link:
                    jobs.append({"corpus": c.name, "build_ids": sorted(c.genes), "metrics": True,
                                 "ov": {"link": link, "kit": kit}, "tag": "sweep", "stage": "build_sweep"})
                    meta.append(("grid", {"link": link, "kit": kit, "corpus": c.name}))
        for fc in FAMILY_CAP_GRID:
            for pk in PLATFORM_KIT_CAP_GRID:
                jobs.append({"corpus": c.name, "build_ids": sorted(c.genes), "metrics": True,
                             "ov": {"FAMILY_CAP": fc, "PLATFORM_KIT_CAP": pk}, "tag": "caps", "stage": "build_sweep"})
                meta.append(("caps", {"FAMILY_CAP": fc, "PLATFORM_KIT_CAP": pk, "corpus": c.name}))
    if not quick and SWEEP_ASSIGN:
        target = corpora[-1]
        for link in LINK_GRID:
            for j in loo_jobs(target, ov={"link": link}):
                jobs.append(j)
                meta.append(("loo", {"link": link}))
            for j in openset_jobs(target, ov={"link": link}):
                jobs.append(j)
                meta.append(("open", {"link": link}))
    results = run_folds(jobs)
    loo_rows: dict[float, list] = defaultdict(list)
    open_rows: dict[float, list] = defaultdict(list)
    for (kind, m), r in zip(meta, results, strict=True):
        if kind in ("grid", "caps") and r["metrics"]:
            row = {**m, **{k: v for k, v in r["metrics"].items() if k != "member_of"}}
            out[kind].append(row)
        elif kind == "loo":
            loo_rows[m["link"]] += r["rows"]
        elif kind == "open":
            open_rows[m["link"]] += r["rows"]
    for link in sorted(loo_rows):
        s = M.summarise_outcomes(loo_rows[link])
        o = M.summarise_outcomes(open_rows.get(link, []))
        out["link_axis"].append({"link": link, "kit": d["kit"], "corpus": corpora[-1].name,
                                 "loo_accuracy": s["accuracy"], "known_accuracy": s["known_accuracy"],
                                 "novel_rejection": s["novel_rejection"], "open_set_rejection": o["novel_rejection"]})
    out["plateau"] = plateau(out["grid"], corpora[0].name)
    return out


def stability(bk: Backend, corpus: Corpus) -> dict:
    """G08: re-cluster under perturbations (link ±2 bits, kit ±2 bits, leave-one-family-out) and measure how
    often each default campaign survives (best-matching cluster Jaccard ≥ 0.8) plus AMI to the default."""
    d = {"link": bk.th.link, "kit": bk.th.kit}
    perts = [("link-2", {"link": d["link"] - 2}, ()), ("link+2", {"link": d["link"] + 2}, ()),
             ("kit-2", {"kit": d["kit"] - 2}, ()), ("kit+2", {"kit": d["kit"] + 2}, ())] + \
        [(f"-{f}", None, (f,)) for f in FAMILIES]
    jobs = [{"corpus": corpus.name, "build_ids": sorted(corpus.genes), "metrics": True, "all_members": True,
             "tag": "stability:default", "stage": "build_stability"}]
    jobs += [{"corpus": corpus.name, "build_ids": sorted(corpus.genes), "metrics": True, "all_members": True,
              "ov": ov, "drop": list(drop), "tag": f"stability:{name}", "stage": "build_stability"}
             for name, ov, drop in perts]
    res = run_folds(jobs)
    if not res[0]["metrics"]:
        return {}
    default = res[0]["metrics"]["member_of"]
    out = {"perturbations": [p[0] for p in perts],
           "all": M.partition_stability(default, [r["metrics"]["member_of"] for r in res[1:] if r["metrics"]]),
           "thresholds_only": M.partition_stability(default, [r["metrics"]["member_of"] for r in res[1:5]
                                                               if r["metrics"]]),
           "leave_one_family_out": M.partition_stability(default, [r["metrics"]["member_of"] for r in res[5:]
                                                                   if r["metrics"]])}
    out["leiden_resolution"] = "not perturbed: no component reaches the size guard on these corpora"
    return out


def fidelity(bk: Backend, corpus: Corpus) -> dict:
    """G11: for every same-campaign pair of the full build, delete the genes behind the top-1 / top-3 stated
    reasons from both incidents, rescore with the same rarity, and count links that no longer hold.
    Sparsity = share of the positive reason bits carried by the top-3 reasons."""
    if not isinstance(bk, HydraBackend):
        return {"note": "CORE backend only"}
    from hydra.synapse import build_genome, score_pair

    b = build_timed(bk, corpus.genes, corpus.labels, stage="build_fidelity")
    rarity, pes = b.ctx["rarity"], b.ctx["pair_evals"]
    linked = [(k, pe) for k, pe in pes.items() if str(pe.score.hypothesis) == "same_campaign"]

    def strip(iid: str, reasons) -> object:
        genes, vecs = bk._data[iid]
        drop = set()
        for r in reasons:
            vals = {v for v in (r.value_a, r.value_b) if v}
            drop |= {id(g) for g in genes if g.kind == r.kind and (not vals or g.value in vals)}
        keep = [g for g in genes if id(g) not in drop]
        refs = {g.vector_ref for g in keep if g.vector_ref}
        return build_genome(iid, keep, {k: v for k, v in vecs.items() if k in refs})

    survive = {1: 0, 3: 0}
    spars = []
    for (x, y), pe in linked:
        reasons = [r for r in pe.score.reasons if r.bits > 0]
        tot = sum(r.bits for r in reasons)
        if tot > 0:
            spars.append(sum(r.bits for r in reasons[:3]) / tot)
        for k in (1, 3):
            top = reasons[:k]
            pe2 = score_pair(strip(x, top), strip(y, top), rarity, bk.th, ["fidelity"])
            survive[k] += str(pe2.score.hypothesis) == "same_campaign"
    n = len(linked)
    return {"linked_pairs": n,
            "links_surviving_top1_removed": round(survive[1] / n, 4) if n else None,
            "links_surviving_top3_removed": round(survive[3] / n, 4) if n else None,
            "fidelity_top1": round(1 - survive[1] / n, 4) if n else None,
            "fidelity_top3": round(1 - survive[3] / n, 4) if n else None,
            "top3_bit_share_mean": round(sum(spars) / len(spars), 4) if spars else None,
            "note": "fidelity = share of links that FAIL once their top-k reasons are deleted (high = the stated "
                    "reasons carry the decision; low = redundant evidence keeps the link)"}


RISK_LINKS = [6.0, 8.0, 10.0, 12.0, 14.0, 16.0]
RISK_ABSTAIN = [3.0, 6.0]
WRONG_LIMIT = 0.02  # pre-set limit: wrong automatic assignments (wrong campaign + foreign assigned) per item


def _risk_point(rows: list[dict]) -> dict:
    s = M.summarise_outcomes(rows)
    n = s["n"] or 0
    auto = n - s["abstain"]
    wrong = s["wrong_campaign"] + s["false_assign"]
    assigned = s["correct_assign"] + wrong
    errors_auto = wrong + s["missed_new"]
    return {"n": n, "automatic": auto, "coverage": round(auto / n, 4) if n else None,
            "correct_assign": s["correct_assign"], "wrong_assign": wrong,
            "precision_automatic_assign": round(s["correct_assign"] / assigned, 4) if assigned else None,
            "precision_ci95": M.wilson(s["correct_assign"], assigned),
            "recall_known": s["known_accuracy"], "recall_known_ci95": s["known_accuracy_ci95"],
            "known_n": s["known_n"], "foreign_n": s["novel_n"],
            "false_assign_rate_foreign": round(s["false_assign"] / s["novel_n"], 4) if s["novel_n"] else None,
            "false_assign_ci95": M.wilson(s["false_assign"], s["novel_n"]),
            "abstention_share": s["abstain_rate"], "abstention_ci95": M.wilson(s["abstain"], n),
            "risk_automatic": round(errors_auto / auto, 4) if auto else None,
            "wrong_rate": round(wrong / n, 4) if n else None}


def campaign_split(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Calibration / test split with no shared campaign, parent or duplicate across it (ASTRA-DELTA-06):
    components = union over (same label) OR (row -> its parent) OR (row -> duplicate_of); whole components are
    assigned, campaign-bearing components alternate in sorted order, the rest alternate separately. The
    intersections are recorded and must be empty (ValueError otherwise).

    CR-ASTRA-SW-06: a parent / duplicate_of id that is not itself a row (an external origin, or a subset holding
    only derivatives) becomes an auxiliary origin node, so siblings of an absent origin still form one component.
    Component roots prefer real row ids, so components without such siblings keep their previous key and order."""
    ids = {r["id"] for r in rows}
    parent = {r["id"]: r["id"] for r in rows}
    aux_prefix = "\x00origin:"  # cannot collide with a row id

    def rank(x: str) -> tuple[bool, str]:
        return (x.startswith(aux_prefix) and x not in ids, x)

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                lo, hi = sorted((ra, rb), key=rank)
                parent[hi] = lo

    first_of_label: dict[str, str] = {}
    for r in rows:
        lab = r.get("label")
        if lab:
            if lab in first_of_label:
                union(r["id"], first_of_label[lab])
            else:
                first_of_label[lab] = r["id"]
        for key in ("parent", "duplicate_of"):
            origin = r.get(key)
            if not origin:
                continue
            if origin in ids:
                union(r["id"], origin)
            else:
                node = aux_prefix + str(origin)
                parent.setdefault(node, node)
                union(r["id"], node)
    comps: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        comps[find(r["id"])].append(r)
    with_c = sorted(k for k, rs in comps.items() if any(is_campaign_label(r.get("label") or "") for r in rs))
    other = sorted(k for k in comps if k not in set(with_c))
    calib_keys = set(with_c[0::2]) | set(other[0::2])
    calib = [r for k in calib_keys for r in comps[k]]
    test = [r for k, rs in comps.items() if k not in calib_keys for r in rs]

    def origins(rs):
        return {r.get("parent") or r["id"] for r in rs}

    def labels(rs):
        return {r.get("label") for r in rs if r.get("label")}

    def dups(rs):
        return {r.get("duplicate_of") or r["id"] for r in rs}

    inter = {"parent_or_origin": len(origins(calib) & origins(test)),
             "campaign_label": len(labels(calib) & labels(test)),
             "duplicate": len(dups(calib) & dups(test))}
    if any(inter.values()):
        raise ValueError(f"campaign_split leaked across calibration/test: {inter}")
    return calib, test, {
        "rule": "components over shared campaign label, parent and duplicate_of; whole components assigned",
        "calib_campaigns": sum(1 for k in with_c if k in calib_keys),
        "test_campaigns": sum(1 for k in with_c if k not in calib_keys),
        "calib_n": len(calib), "test_n": len(test),
        "intersections": inter}


def selective_risk(bk: Backend, calib: Corpus, test: Corpus) -> dict:
    """(1) Risk-coverage over link x abstain thresholds; thresholds chosen on CALIBRATION (max correct automatic
    assignments with wrong-assignment rate <= WRONG_LIMIT, ties to the higher link), FROZEN, then reported on
    TEST with the ±1/±2-bit neighbourhood. Both splits are SYNTHETIC-derived and campaign-disjoint."""
    grid = [(lk, ab) for lk in RISK_LINKS for ab in RISK_ABSTAIN]
    jobs, meta = [], []
    for corpus in (calib, test):
        for lk, ab in grid:
            for j in loo_jobs(corpus, ov={"link": lk, "abstain_min": ab}):
                jobs.append(j)
                meta.append((corpus.name, lk, ab))
    res = run_folds(jobs)
    rows: dict[tuple, list] = defaultdict(list)
    for m, r in zip(meta, res, strict=True):
        rows[m] += r["rows"]
    curve = {c.name: [{"link": lk, "abstain_min": ab, **_risk_point(rows[(c.name, lk, ab)])} for lk, ab in grid]
             for c in (calib, test)}
    ok = [p for p in curve[calib.name] if p["wrong_rate"] is not None and p["wrong_rate"] <= WRONG_LIMIT]
    chosen = max(ok, key=lambda p: (p["correct_assign"], p["link"])) if ok else None
    out = {"wrong_limit": WRONG_LIMIT, "grid": {"link": RISK_LINKS, "abstain_min": RISK_ABSTAIN},
           "curve": curve, "chosen_on_calibration": chosen and {"link": chosen["link"],
                                                                "abstain_min": chosen["abstain_min"]}}
    if chosen:
        tp = {(p["link"], p["abstain_min"]): p for p in curve[test.name]}
        out["test_at_frozen"] = tp.get((chosen["link"], chosen["abstain_min"]))
        out["test_neighbourhood"] = [p for p in curve[test.name] if p["abstain_min"] == chosen["abstain_min"]
                                     and abs(p["link"] - chosen["link"]) <= 2.0]
        out["test_at_defaults"] = tp.get((bk.th.link, 6.0))
    return out


def conformal_stages(rows: list[dict]) -> dict:
    """(3) Per item: retrieval found the true campaign? -> in the conformal set? -> survived the evidence gate
    (correct assignment)? Raw-set rejection vs final rejection for foreign items. No coverage claim."""
    known = [r for r in rows if r["label_known"]]
    novel = [r for r in rows if not r["label_known"]]
    k_ret = sum(1 for r in known if r["true_label"] in (r.get("cand_labels") or []))
    k_set = sum(1 for r in known if r["true_label"] in (r.get("conformal_set_labels") or []))
    k_fin = sum(1 for r in known if r["outcome"] == "correct_assign")
    n_raw = sum(1 for r in novel if not r.get("conformal_set_labels"))
    n_fin = sum(1 for r in novel if r["outcome"] == "correct_new")
    w = M.wilson
    return {"known_n": len(known), "retrieved_true_campaign": k_ret, "retrieved_ci95": w(k_ret, len(known)),
            "true_campaign_in_conformal_set": k_set, "in_set_ci95": w(k_set, len(known)),
            "final_correct_assign": k_fin, "final_ci95": w(k_fin, len(known)),
            "foreign_n": len(novel), "raw_set_empty": n_raw, "raw_set_rejection_ci95": w(n_raw, len(novel)),
            "final_rejection": n_fin, "final_rejection_ci95": w(n_fin, len(novel)),
            "decided_by": dict(Counter(r.get("decided_by", "threshold") for r in rows)),
            "note": "conformal coverage is marginal over calibration items of the build; no nominal coverage is "
                    "claimed for the whole pipeline"}


MECHANISMS = ["no_rarity", "no_tiers", "no_caps", "no_guards"]


def mechanism_ablation(bk: Backend, corpus: Corpus) -> dict:
    """(4) Mechanism ablations: uniform bits, no tiers, no dependency-group pooling / family caps, no graph
    consistency guards. Full build (campaign + group metrics) and LOO (missed variants, abstention)."""
    jobs, owner = [], []
    for mech in MECHANISMS:
        jobs.append({"corpus": corpus.name, "build_ids": sorted(corpus.genes), "metrics": True, "ov": {"mech": mech},
                     "tag": f"mech:{mech}", "stage": "build_mech"})
        owner.append((mech, "full"))
        for j in loo_jobs(corpus, ov={"mech": mech}):
            jobs.append(j)
            owner.append((mech, "loo"))
    res = run_folds(jobs)
    out = {}
    for mech in MECHANISMS:
        full = [r for (m, k), r in zip(owner, res, strict=True) if m == mech and k == "full"][0]["metrics"] or {}
        rows = [x for (m, k), r in zip(owner, res, strict=True) if m == mech and k == "loo" for x in r["rows"]]
        s = M.summarise_outcomes(rows)
        out[mech] = {"campaign_f1": full.get("campaign_f1"), "false_merges": full.get("false_merges"),
                     "group_f1": full.get("group_f1"), "group_false_merges": full.get("group_false_merges"),
                     "loo_accuracy": s["accuracy"], "missed_known": s["missed_new"] + s["wrong_campaign"],
                     "known_n": s["known_n"], "abstain": s["abstain"], "n": s["n"]}
    return out


def retrieval_test(big: Corpus) -> dict:
    """CORE's real candidate filtering (n > all_pairs_below = 60) vs forced all pairs on the same corpus:
    candidate recall of labelled same-campaign pairs, false merges, build wall time, peak RSS increase of the
    worker during the build (psutil sampler, 0.2 s)."""
    ids = sorted(big.genes)
    jobs = [{"corpus": big.name, "build_ids": ids, "retrieval": True, "tag": "retrieval-filtered",
             "ov": {"all_pairs_below": 60}, "stage": "build_retrieval"},
            {"corpus": big.name, "build_ids": ids, "retrieval": True, "tag": "retrieval-allpairs",
             "ov": {"all_pairs_below": 10**9}, "stage": "build_retrieval"}]
    r = run_folds(jobs)
    kinds = Counter((big.refs[i].meta or {}).get("kind", "sample") for i in ids)
    return {"filtered": r[0]["metrics"], "all_pairs": r[1]["metrics"], "composition": dict(kinds),
            "note": "SYNTHETIC-heavy corpus: sample + syn-dev + sample+syn + robustness variants; labels inherited"}


def temporal_replay(bk: Backend, sample: Corpus) -> dict:
    """Time-respecting (past-only) replay: incidents sorted by observed_at; each is assigned against a build of
    the incidents seen before it, so rarity never sees the future (G09). Undated incidents go last."""
    obs = getattr(bk, "observed_at", {})
    order = sorted(sample.genes, key=lambda i: (obs.get(i) or "9999", i))
    if sum(1 for i in order if obs.get(i)) < 2:
        return {"note": "fewer than 2 dated incidents"}
    jobs = [{"corpus": sample.name, "build_ids": order[:k], "assign_ids": [order[k]], "tag": f"replay:{k}",
             "stage": "build_replay"} for k in range(1, len(order))]
    rows = [row for f in run_folds(jobs) for row in f["rows"]]
    for row in rows:
        row["observed_at"] = obs.get(row["incident"])
    months = sorted({(obs.get(i) or "")[:7] for i in order if obs.get(i)})
    return {"order": [(i, obs.get(i)) for i in order], "summary": _summ(rows), "rows": rows,
            "dated": sum(1 for i in order if obs.get(i)), "months": months,
            "aut_curve": "not computed: fewer than 3 months of dates" if len(months) < 3 else None}


def sweep_consistency(res: dict) -> dict:
    """The sweep's default point must reproduce the main system numbers (else builds depend on hidden state)."""
    sw, sysm = res.get("core_sweep") or {}, res.get("system", {})
    d = sw.get("defaults", {})
    out = {}
    for r in sw.get("grid", []):
        if r["link"] == d.get("link") and r["kit"] == d.get("kit") and r["corpus"] in sysm:
            g = sysm[r["corpus"]]["grouping"]["campaign"]["pairwise"]
            ok = r["campaign_f1"] == g["f1"] and r["false_merges"] == g["fp"]
            out[r["corpus"]] = {"sweep_f1": r["campaign_f1"], "system_f1": g["f1"], "sweep_fm": r["false_merges"],
                                "system_fm": g["fp"], "ok": ok}
    return out


def plateau(grid: list[dict], corpus: str) -> dict:
    """Most stable (link, kit): best mean of (campaign F1 + group F1)/2 − 0.01·false merges over the point and its
    grid neighbours (dev corpus). Ties keep the first point in grid order."""
    pts = {(r["link"], r["kit"]): r for r in grid if r["corpus"] == corpus}
    links, kits = sorted({k[0] for k in pts}), sorted({k[1] for k in pts})
    best = None
    for (li, ki), r in pts.items():
        i, j = links.index(li), kits.index(ki)
        neigh = [pts[(links[x], kits[y])] for x in (i - 1, i, i + 1) for y in (j - 1, j, j + 1)
                 if 0 <= x < len(links) and 0 <= y < len(kits) and (links[x], kits[y]) in pts]
        score = sum((n["campaign_f1"] + n["group_f1"]) / 2 - 0.01 * n["false_merges"] for n in neigh) / len(neigh)
        if best is None or score > best[0]:
            best = (score, li, ki, r)
    if best is None:
        return {}
    return {"corpus": corpus, "link": best[1], "kit": best[2], "neighbourhood_score": round(best[0], 4),
            "campaign_f1_at_point": best[3]["campaign_f1"]}


# --------------------------------------------------------------------------------------- persistence


def write_db(metrics: dict, run_id: str, params: dict, db_path: Path) -> str:
    from hydra.db import Writer
    from hydra.events import canonical_json, utcnow

    w = Writer(db_path)
    try:
        def tx(conn):
            conn.execute("INSERT OR IGNORE INTO runs (id, kind, params, metrics, created_at) VALUES (?,?,?,?,?)",
                         (run_id, "eval", canonical_json(params), "{}", utcnow()))
            conn.execute("UPDATE runs SET metrics = ?, params = ?, finished_at = ? WHERE id = ?",
                         (canonical_json(metrics), canonical_json(params), utcnow(), run_id))
            return conn.execute("SELECT length(metrics) FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        n = w.run(tx)
    finally:
        w.close()
    return f"{db_path} runs[{run_id}] metrics {n} bytes"


PRIVATE_KEYS = {"rows", "curve", "trace", "items", "predicted_campaigns", "sample_build", "order", "label_status",
                "disputed_pairs", "labels", "state_dir", "db", "member_of", "held_out"}


def aggregate_metrics(res: dict) -> dict:
    """Aggregate numbers only (no incident ids, file names, paths or per-incident rows): safe to commit as
    `eval/published/metrics.json` so the UI has methodology numbers on a fresh install."""
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in PRIVATE_KEYS}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    out = strip(res)
    out.setdefault("meta", {})["published"] = ("aggregate metrics of an eval run; per-incident rows and identifiers "
                                               "removed (eval/run.py aggregate_metrics)")
    out["crashes"] = {"count": res.get("crashes", {}).get("count", 0)}
    hn = out.get("system", {})
    for v in list(hn.values()) + [b for by in out.get("system_alt", {}).values() if isinstance(by, dict)
                                  for b in by.values() if isinstance(b, dict)]:
        if isinstance(v, dict) and isinstance(v.get("hard_negatives"), dict):
            keep = ("pairs", "violations_campaign", "violations_group")
            v["hard_negatives"] = {k: v["hard_negatives"][k] for k in keep if k in v["hard_negatives"]}
    return out


def ui_metrics(res: dict) -> dict:
    """Compact metrics for the UI (no per-incident rows)."""
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in {"rows", "curve", "trace"}}
        return x
    return strip(res)


# --------------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto", choices=["auto", "stub", "hydra"])
    ap.add_argument("--sample", type=Path, default=NASK / "sample")
    ap.add_argument("--labels", type=Path, default=NASK / "labels" / "sample_labels.json")
    ap.add_argument("--variants", type=Path, default=NASK / "variants")
    ap.add_argument("--out", type=Path, default=EVAL_DIR)
    ap.add_argument("--json-out", type=Path, default=None, help="metrics JSON path (default <out>/results.json)")
    ap.add_argument("--db", type=Path, default=None, help="SQLite for runs.metrics (default: the eval state DB)")
    ap.add_argument("--state-dir", type=Path, default=None, help="isolated eval HYDRA state (default "
                    "~/HydraNexus/cache/eval-state; never the real state)")
    ap.add_argument("--fresh-state", action="store_true", help="use a new tmp state dir (re-extracts everything)")
    ap.add_argument("--link", type=float, default=None, help="CORE same-campaign threshold (bits)")
    ap.add_argument("--kit", type=float, default=None, help="CORE campaign-group threshold (bits)")
    ap.add_argument("--no-sweep", action="store_true", help="skip the CORE threshold/cap sweep")
    ap.add_argument("--sweep-assign", action="store_true", help="also LOO + open set along the link axis")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip ablation LOO and the ladder LOO")
    ap.add_argument("--regen", action="store_true", help="regenerate synthetic sets")
    ap.add_argument("--hydra-root", default=None)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--publish-artefact", action="store_true",
                    help="also write eval/published/metrics.json (aggregate numbers only, committed for the UI)")
    ap.add_argument("--workers", type=int, default=1, help="fold-job process pool (CORE; needs --extract-workers>1)")
    ap.add_argument("--extract-workers", type=int, default=1, help="parallel CORE extraction shards (subprocesses)")
    ap.add_argument("--tmp-root", type=Path, default=None, help="root for shard states (default /tmp/hydra-eval-$USER)")
    ap.add_argument("--fusion", type=Path, default=None, help="pretrained fusion model (<state>/models/fusion/*.pkl "
                    "from `hydra train` on external corpora) used by the system scorer; default: none")
    a = ap.parse_args(argv)
    t_start = time.perf_counter()
    globals()["SWEEP_ASSIGN"] = a.sweep_assign
    CRASHES.clear()
    _BUILDS.clear()
    T.t.clear()
    T.n.clear()

    labels_doc = L.load(a.labels)  # applies the review mask of two-reviewer label files (eval/labels.py)
    sample_rows = V.load_sample(a.sample, a.labels)
    CAMPAIGN_LABELS.clear()
    CAMPAIGN_LABELS.update(r["label"] for r in sample_rows if r.get("label"))
    for setname, seed in (("set-s1", 1), ("set-s2", 2)):
        if a.regen or not (a.variants / setname / "manifest.json").exists():
            V.generate_set(sample_rows, a.variants / setname, seed)
    s1 = relabel(uniq_ids(relocate(json.loads((a.variants / "set-s1" / "manifest.json").read_text())["incidents"],
                                   a.variants), "s1"), sample_rows)
    s2 = relabel(uniq_ids(relocate(json.loads((a.variants / "set-s2" / "manifest.json").read_text())["incidents"],
                                   a.variants), "s2"), sample_rows)
    # per-run robustness directory: concurrent runs (other agents) regenerate these folders and race otherwise
    rob_dir = (a.tmp_root / "robustness") if a.tmp_root else a.variants / "robustness"
    rob = robustness_set(sample_rows, rob_dir)
    rob_rows = [r for rows in rob.values() for r in rows]
    noenrich_rows = [{"id": f"rob-noenrich-{r['id']}", "path": r["path"], "label": r["label"],
                      "group_label": r["group_label"], "parent": r["id"], "kind": "enrich_unreachable",
                      "synthetic": True, "mutations": ["empty enrichment cache, live lookups off"]}
                     for r in sample_rows]

    stub = StubBackend("B3_rarity_bits")
    state_dir = Path(tempfile.mkdtemp(prefix="hydra-eval-state-")) if a.fresh_state else (a.state_dir or
                                                                                         default_eval_state())
    th = {k: v for k, v in (("link", a.link), ("kit", a.kit)) if v is not None}
    primary = make_backend(a.backend, state_dir=state_dir, thresholds=th,
                           **({"fusion": a.fusion} if a.fusion else {}))
    try:  # --hydra-root may point at a checkout without the shipped prior (eval-gate vs main)
        from hydra import priors as _priors

        bg_prior = _priors.describe()
    except ImportError:
        bg_prior = {"enabled": False, "mode": "absent"}

    def _git(*args: str) -> str | None:
        try:
            import subprocess

            return subprocess.run(["git", *args], cwd=EVAL_DIR.parent, capture_output=True, text=True,
                                  check=True).stdout.strip()
        except Exception:  # noqa: BLE001 — exported trees (eval-gate) have no git
            return None

    commit = {"checkout": _git("rev-parse", "HEAD"), "hydra_dirty": bool(_git("status", "--porcelain", "hydra")),
              "hydra_root_override": HYDRA_ROOT_OVERRIDE, "product": os.environ.get("EVAL_PRODUCT_COMMIT")}
    res: dict = {"meta": {"backend": primary.name, "background_prior": bg_prior, "labels": str(a.labels),
                          "commit": commit,
                          "labels_decided_by": labels_doc["_meta"].get("decided_by"),
                          "label_mask": L.summary(labels_doc),
                          "synthetic_note": V.SYNTHETIC_NOTE, "quick": a.quick,
                          "corpora": {"sample": "10 organiser incidents (REAL)",
                                      "sample+syn": f"sample + {len(s2)} SYNTHETIC incidents (set-s2)",
                                      "syn-dev": f"{len(s1)} SYNTHETIC incidents (set-s1), threshold tuning only"}}}

    is_core = isinstance(primary, HydraBackend)
    sweep = is_core and not a.no_sweep
    res["meta"]["corpus_fingerprint"] = guarded(
        "corpus_fingerprint", corpus_fingerprint, sample_rows + s2,
        [a.variants / "set-s1" / "manifest.json", a.variants / "set-s2" / "manifest.json"])
    log("extracting corpora")
    if is_core and a.extract_workers > 1:
        tmp_root = a.tmp_root or Path(tempfile.gettempdir()) / f"hydra-eval-{os.environ.get('USER', 'x')}"
        todo = sample_rows + s2 + rob_rows + s1
        res["meta"]["extract"] = parallel_extract(primary, todo, noenrich_rows, tmp_root, a.extract_workers)
    dev = Corpus("syn-dev", refs_from_rows(s1), stub)
    sample = Corpus("sample", refs_from_rows(sample_rows), primary)
    mixed = Corpus("sample+syn", refs_from_rows(sample_rows + s2), primary)
    rob_corpus = Corpus("robust-variants", refs_from_rows(rob_rows), primary)
    if is_core:
        if not (a.extract_workers > 1):
            extract_noenrich_serial(primary, noenrich_rows, state_dir)
        rob_corpus = Corpus("robust-variants", refs_from_rows(rob_rows + noenrich_rows), primary)
    robust = robust_corpus(mixed, rob_corpus)
    dev_core = Corpus("syn-dev", refs_from_rows(s1), primary) if is_core else dev
    alts = labels_doc["_meta"].get("alternative_label_sets", {})
    alt_corpora = {(alt, c.name): c.relabelled(spec["incidents"], f"@{alt}") for alt, spec in alts.items()
                   for c in (sample, mixed)}
    for c in (sample, mixed):  # disputed = relation differs between label sets: never an automatic negative
        c.disputed = M.disputed_pairs([c.labels] + [alt_corpora[(alt, c.name)].labels for alt in alts])
        for alt in alts:
            alt_corpora[(alt, c.name)].disputed = c.disputed
    robust.disputed = mixed.disputed
    res["meta"]["label_status"] = {i: {"label": v.get("label_status"), "group": v.get("group_status")}
                                   for i, v in labels_doc["incidents"].items()}
    res["meta"]["disputed_pairs"] = {c.name: sorted(map(list, c.disputed)) for c in (sample,)}
    # retrieval test above CORE's all-pairs cut-off (60): every incident the harness has
    big = None
    if is_core:
        big = Corpus.__new__(Corpus)
        big.name, big.disputed = "retrieval", set()
        big.refs = {**dev_core.refs, **robust.refs}
        big.genes = {**dev_core.genes, **robust.genes}
        big.labels = {**dev_core.labels, **robust.labels}
        big.groups = {**dev_core.groups, **robust.groups}
    risk_corpora, split_info = [], {}
    if is_core:  # campaign- and origin-disjoint calibration / test split for the selective-risk protocol
        calib_rows, test_rows, split_info = campaign_split(sample_rows + s1 + s2)
        pool_refs, pool_genes = {**dev_core.refs, **mixed.refs}, {**dev_core.genes, **mixed.genes}
        for rr, nm in ((calib_rows, "risk-calib"), (test_rows, "risk-test")):
            c = Corpus.__new__(Corpus)
            ids = [r["id"] for r in rr if r["id"] in pool_refs]
            c.name, c.disputed = nm, set()
            c.refs = {i: pool_refs[i] for i in ids}
            c.genes = {i: pool_genes[i] for i in ids}
            c.labels = {i: r.label for i, r in c.refs.items() if r.label}
            c.groups = {i: r.group_label for i, r in c.refs.items() if r.group_label}
            risk_corpora.append(c)
    for c in [sample, mixed, robust, dev_core, *alt_corpora.values(), *risk_corpora] + ([big] if big else []):
        register(c)
    if is_core:
        res["meta"]["state_dir"] = str(state_dir).replace(str(Path.home()), "~")
        res["meta"]["core_extract"] = {"incidents": len(primary.extract_stats),
                                       "cached": sum(1 for v in primary.extract_stats.values() if v.get("cached"))}

    log("tuning ladder thresholds")
    # Same features as the system (CORE genes when CORE is evaluated) and the same budget: one threshold per
    # method, chosen on syn-dev from its grid by max campaign pairwise F1 (ties → higher threshold).
    tuned = {name: tune(name, dev_core) for name in LADDER}
    ladder_backends = {name: StubBackend(name, tuned[name]["threshold"], tuned[name]["group_threshold"])
                       for name in LADDER}
    CTX["backends"].update(ladder_backends)
    res["thresholds"] = {k: {kk: vv for kk, vv in v.items() if kk != "curve"} for k, v in tuned.items()}
    res["threshold_curves"] = {k: v["curve"] for k, v in tuned.items()}
    if isinstance(primary, StubBackend):
        primary = StubBackend("B3_rarity_bits", tuned["B3_rarity_bits"]["threshold"],
                              tuned["B3_rarity_bits"]["group_threshold"])
        res["meta"]["backend"] = primary.name + " (EVAL STUB scorer, not CORE)"
    CTX["backend"] = primary
    if is_core and a.workers > 1:
        if a.extract_workers > 1:
            start_pool(a.workers)
            res["meta"]["workers"] = a.workers
            log(f"fold pool started: {a.workers} workers")
        else:
            log("--workers ignored: the parent ran CORE extraction itself (torch/libgomp is not fork-safe)")

    # main system: grouping, LOO, open set, hard negatives — under the adopted labels and every alternative set
    def system_eval(corpus: Corpus) -> tuple[dict, S.Build]:
        b = build_timed(primary, corpus.genes, corpus.labels, stage=f"build_full[{corpus.name}]")
        g = grouping_metrics(b, corpus)
        g["campaign"]["bootstrap"] = M.bootstrap_grouping(b.member_of, corpus.labels)
        g["group"]["bootstrap"] = M.bootstrap_grouping(b.group_of, corpus.groups)
        g["campaign"]["connectivity"] = M.connectivity_recall(set(b.pairs), corpus.labels)
        hn = hard_negatives(b, labels_doc["_meta"])
        hn["violations_ci95"] = M.wilson(hn["violations_campaign"], hn["pairs"])
        return {"grouping": g, "loo": leave_one_out(primary, corpus),
                "open_set": open_set(primary, corpus), "hard_negatives": hn,
                "predicted_campaigns": [c.members for c in b.campaigns if len(c.members) > 1]}, b

    log("system eval")
    for corpus in (sample, mixed):
        res.setdefault("system", {})[corpus.name], b = system_eval(corpus)
        if corpus is sample:
            sample_build = b
    for alt, spec in alts.items():
        res.setdefault("system_alt", {})[alt] = {"note": spec.get("note")}
        for corpus in (sample, mixed):
            out, _ = system_eval(alt_corpora[(alt, corpus.name)])
            for k in ("loo", "open_set"):
                out[k] = {"summary": out[k]["summary"]}
            res["system_alt"][alt][corpus.name] = out
    loo_by_parent = {r["incident"]: r for r in res["system"]["sample+syn"]["loo"]["rows"]}

    log("ladder")
    # ladder (B0..LR) on both corpora, same genes as the system
    for name in LADDER:
        bk = ladder_backends[name]
        for corpus in (sample, mixed):
            key = corpus.name
            b = bk.build(corpus.genes, corpus.labels)
            g = grouping_metrics(b, corpus)
            row = {"campaign_pairwise": g["campaign"]["pairwise"], "campaign_bcubed": g["campaign"]["bcubed"],
                   "group_pairwise": g["group"]["pairwise"], "threshold": tuned[name]["threshold"]}
            row["genes"] = "CORE" if is_core else "stub"
            if not a.quick or corpus is sample:
                loo = leave_one_out(bk, corpus)
                row["loo_accuracy"] = loo["summary"]["accuracy"]
                row["open_set_rejection"] = open_set(bk, corpus)["summary"]["rejection_rate"]
            if name == "LR_fusion":
                row["lr_fallback"] = bool(getattr(b.ctx, "fallback", False))
            res.setdefault("ladder", {}).setdefault(name, {})[key] = row

    log("robustness")
    if is_core:  # the system itself is the top rung: CORE rarity bits with tiers and caps
        for corpus in (sample, mixed):
            v = res["system"][corpus.name]
            g = v["grouping"]
            res["ladder"].setdefault("B3_CORE_rarity_bits_tiers_caps", {})[corpus.name] = {
                "campaign_pairwise": g["campaign"]["pairwise"], "campaign_bcubed": g["campaign"]["bcubed"],
                "group_pairwise": g["group"]["pairwise"], "threshold": primary.th.link,
                "loo_accuracy": v["loo"]["summary"]["accuracy"],
                "open_set_rejection": v["open_set"]["summary"]["rejection_rate"], "genes": "CORE",
                "threshold_source": "CORE default"}

    # robustness on the mixed corpus (variants built from every sample incident)
    res["robustness"] = robustness(primary, mixed, robust, loo_by_parent)
    if not is_core:
        res["robustness"]["enrich_unreachable"] = "not applicable: the eval stub uses no enrichment"
    if sweep:
        log("core sweep")
        res["core_sweep"] = guarded("core_sweep", core_sweep, primary, [dev_core, mixed], a.quick)
        if res["core_sweep"]:
            res["core_sweep"]["consistency"] = sweep_consistency(res)
            # CORE with the same tuning budget as the ladder: link chosen on syn-dev (kit at default)
            dev_pts = [r for r in res["core_sweep"]["grid"] if r["corpus"] == "syn-dev" and r["kit"] == primary.th.kit]
            if dev_pts:
                best = max(dev_pts, key=lambda r: (r["campaign_f1"], r["link"]))
                ov = {"link": best["link"]}
                for corpus in (sample, mixed):
                    with _Overrides(primary, ov):
                        _BUILDS.clear()
                        b = build_timed(primary, corpus.genes, corpus.labels, stage="build_tuned")
                        g = grouping_metrics(b, corpus)
                    loo = leave_one_out(primary, corpus, ov=ov)
                    res["ladder"].setdefault("CORE_tuned_on_syn_dev", {})[corpus.name] = {
                        "campaign_pairwise": g["campaign"]["pairwise"], "campaign_bcubed": g["campaign"]["bcubed"],
                        "group_pairwise": g["group"]["pairwise"], "threshold": best["link"],
                        "loo_accuracy": loo["summary"]["accuracy"],
                        "open_set_rejection": open_set(primary, corpus, ov=ov)["summary"]["rejection_rate"],
                        "genes": "CORE", "threshold_source": "syn-dev (same budget as the ladder)"}
                _BUILDS.clear()

    log("ablation")
    # ablation + coverage
    if not a.quick:
        res["ablation"] = {"corpus": "sample+syn",
                           **ablation(primary, mixed, res["system"]["sample+syn"]["loo"],
                                      res["system"]["sample+syn"]["grouping"])}
    if is_core and not a.quick:
        log("selective risk, conformal stages, mechanism ablations")
        if risk_corpora:
            res["selective_risk"] = {"split": split_info,
                                     **(guarded("selective_risk", selective_risk, primary, CTX["corpora"]["risk-calib"],
                                                CTX["corpora"]["risk-test"]) or {})}
        res["conformal_stages"] = {c: conformal_stages(res["system"][c]["loo"]["rows"]) for c in ("sample",
                                                                                              "sample+syn")}
        res["mechanism_ablation"] = {"corpus": "sample+syn",
                                     **(guarded("mechanisms", mechanism_ablation, primary, mixed) or {})}
    if big is not None and not a.quick:
        log(f"retrieval test: {len(big.genes)} incidents")
        res["retrieval"] = retrieval_test(big)
    if is_core:
        log("temporal replay, stability, fidelity")
        res["temporal_replay"] = temporal_replay(primary, sample)
        res["temporal_replay_all"] = {}
        for corpus in (sample, mixed):
            tr = temporal_replay(primary, corpus)
            if "summary" in tr:
                loo = res["system"][corpus.name]["loo"]["summary"]
                tr = {"summary": tr["summary"], "dated": tr["dated"], "months": tr["months"],
                      "loo_accuracy": loo["accuracy"],
                      "past_only_minus_loo": round((tr["summary"]["accuracy"] or 0) - (loo["accuracy"] or 0), 4)}
            res["temporal_replay_all"][corpus.name] = tr
        res["stability"] = {c.name: guarded(f"stability:{c.name}", stability, primary, c) for c in (sample, mixed)}
        res["fidelity"] = {c.name: guarded(f"fidelity:{c.name}", fidelity, primary, c) for c in (sample, mixed)}
    res["coverage"] = {"sample": coverage(sample), "sample+syn": coverage(mixed)}
    res["runtime"] = {**T.summary(), "wall_s": round(time.perf_counter() - t_start, 2)}
    res["crashes"] = {"count": len(CRASHES), "items": CRASHES[:50]}
    res["sample_build"] = {"campaigns": [{"id": c.id, "members": c.members, "group_id": getattr(c, "group_id", None)}
                                         for c in sample_build.campaigns]}

    res["headline"] = headline(res)
    run_id = f"eval-{time.strftime('%Y%m%dT%H%M%S')}"
    res["meta"]["run_id"] = run_id
    a.out.mkdir(parents=True, exist_ok=True)
    jpath = a.json_out or (a.out / "results.json")
    jpath.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    if a.publish_artefact:
        pub = EVAL_DIR / "published" / "metrics.json"
        pub.parent.mkdir(exist_ok=True)
        pub.write_text(json.dumps(aggregate_metrics(res), indent=1, ensure_ascii=False, default=str))
    if not a.no_report:
        from eval import report

        report.write(res, a.out)

    jpath.write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    if not a.no_db:
        from hydra.config import Settings

        db = a.db or (primary.settings.db_path if is_core else Settings(state_dir=state_dir).db_path)
        res["meta"]["db"] = guarded("write_db", write_db, ui_metrics(res), run_id,
                                    {"backend": res["meta"]["backend"], "quick": a.quick}, db)
    print(json.dumps(res["headline"], indent=1))
    stop_pool()
    if is_core:
        primary.close()
    return res


def harness_identity(res: dict) -> dict:
    """What must match for two headlines to be comparable in eval.gate (CR-ASTRA-PIPE-202): quick/full mode and
    the labels file content (sha256, no path). Aggregates only, no incident identifiers."""
    meta = res.get("meta") or {}
    labels = Path(str(meta.get("labels") or ""))
    digest = hashlib.sha256(labels.read_bytes()).hexdigest()[:16] if labels.is_file() else None
    return {"quick": bool(meta.get("quick")), "labels_sha256": digest,
            "corpus_sha256": (meta.get("corpus_fingerprint") or {}).get("sha256")}


def corpus_fingerprint(rows: list[dict], manifests: list[Path]) -> dict:
    """Content identity of the evaluated corpus (CR-202 / U00.2b): sorted incident ids, each with the sorted sha256
    of its files, plus the sha256 of the synthetic manifests. Ids are only hashed (no identifiers exported)."""
    import tempfile as _tf

    from eval.incidents import materialise

    h = hashlib.sha256()
    scratch = Path(_tf.mkdtemp(prefix="hydra-eval-fp-"))
    for r in sorted(rows, key=lambda r: r["id"]):
        inc = materialise(Path(r["path"]), r["id"], scratch / r["id"])
        h.update(r["id"].encode() + b"\0" + "\n".join(sorted(f.sha256 for f in inc.files)).encode() + b"\n")
    for mp in manifests:
        h.update(hashlib.sha256(mp.read_bytes()).hexdigest().encode() if mp.exists() else b"missing")
    return {"sha256": h.hexdigest()[:24], "incidents": len(rows), "manifests": [m.name for m in manifests]}


def headline(res: dict) -> dict:
    out = {"backend": res["meta"]["backend"], "crashes": res["crashes"]["count"], "harness": harness_identity(res)}
    for c, v in res["system"].items():
        out[c] = {"campaign_pairwise_f1": v["grouping"]["campaign"]["pairwise"]["f1"],
                  "campaign_bcubed_f1": v["grouping"]["campaign"]["bcubed"]["f1"],
                  "group_pairwise_f1": v["grouping"]["group"]["pairwise"]["f1"],
                  "false_merge_pairs": v["grouping"]["campaign"]["pairwise"]["fp"],
                  "loo_accuracy": v["loo"]["summary"]["accuracy"],
                  "loo_known_accuracy": v["loo"]["summary"]["known_accuracy"],
                  "open_set_rejection": v["open_set"]["summary"]["rejection_rate"],
                  "hard_negative_violations": v["hard_negatives"]["violations_campaign"],
                  "hard_negative_violations_group": v["hard_negatives"]["violations_group"],
                  "open_set_group_level_correct": v["open_set"]["summary"].get("group_level_correct"),
                  "open_set_sent_to_sibling_campaign": v["open_set"]["summary"].get(
                      "false_assign_to_sibling_in_same_group"),
                  "loo_group_level_accuracy": (v["loo"]["summary"].get("group_level") or {}).get("accuracy")}
    for alt, by in res.get("system_alt", {}).items():
        for c, v in by.items():
            if c != "note":
                out[f"{alt}:{c}"] = {"campaign_pairwise_f1": v["grouping"]["campaign"]["pairwise"]["f1"],
                                     "false_merge_pairs": v["grouping"]["campaign"]["pairwise"]["fp"],
                                     "loo_accuracy": v["loo"]["summary"]["accuracy"],
                                     "open_set_rejection": v["open_set"]["summary"]["rejection_rate"],
                                     "open_set_sent_to_sibling_campaign": v["open_set"]["summary"].get(
                                         "false_assign_to_sibling_in_same_group")}
    return out


if __name__ == "__main__":
    main()
