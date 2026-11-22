"""Incremental, event-driven per-run aggregates (DATAFLOW, ADR-0009).

An analyse run is an immutable snapshot (the *base*). What is attached to it later is a *delta*: assign runs with
`params.base_run = run` (upload, `assign-batch`, scheduler inbox), analyst reviews (pair decisions, "niepewne"
marks) and exports. This module keeps materialised aggregates per run in the `agg_*` tables (migration v5):

- `build(db_path, writer, run)` computes the base aggregates on a read-only connection (off the writer thread) and
  installs them in one short writer transaction, which also catches up with every delta that already exists;
- `on_event(conn, seq, type, payload)` is called by `hydra.events.append` inside the writer transaction of the
  event itself and applies the delta (no recomputation: only the touched counters, gene values, campaigns and
  campaign-to-campaign links change), bumps the run's `version`, and stores a compact delta message in
  `agg_deltas` (the live stream replays and forwards those, keyed by `events.seq`);
- `recompute(conn, run)` is the reference: every aggregate from scratch. `snapshot(conn, run)` reads the
  materialised state in the same shape, so tests assert incremental == full recomputation.

Counters are `[n, n_new]`: current total (base + new material) and the part contributed by material that is new
since the analysis ("nowe od analizy"). Nothing here estimates anything: every number is a count or a copy of a
stored value (`pairs.total_bits` rounded as in the graph API).

`HYDRA_DATAFLOW=0` disables the taps (the API then falls back to the per-run cache semantics of API-PERF).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson

from hydra import events

log = logging.getLogger("hydra.dataflow")

UNIT = "\x1f"
LEVEL_OF = {"same_campaign": "campaign", "shared_template": "kit", "shared_asset": "asset", "abstain": "weak"}
KIT_BITS = 6.0  # synapse.Thresholds.kit, as hydra.api_graph.KIT_BITS
LEVEL_RANK = {"review": 5, "campaign": 4, "kit": 3, "asset": 2, "weak": 1}
REVIEW_OF = {"accepted": "must_link", "rejected": "cannot_link", "unsure": "unsure"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
THUMB_ORDER = {"ad_creative": 0, "feed_screenshot": 1, "landing_screenshot": 2}
TOP_GENES = 20
DELTA_KEEP = 20000  # stored delta messages per run (older ones: a resuming client gets `reset`)
AGG_TABLES = ("agg_counts", "agg_incidents", "agg_genes", "agg_pairs", "agg_campaigns", "agg_links")
EXPORT_EVENT = "EXPORT"
UNSURE_EVENTS = ("REVIEW_UNSURE", "REVIEW_UNSURE_UNDONE")


def enabled() -> bool:
    return os.environ.get("HYDRA_DATAFLOW", "1") != "0"


def _dumps(obj: Any) -> str:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode()


def _loads(text: Any) -> Any:
    if not text:
        return {}
    try:
        return orjson.loads(text)
    except orjson.JSONDecodeError:
        return {}


def corpus_of(source: str | None) -> str:
    try:
        from hydra.corpus import corpus_of as _c

        return _c(source)
    except ImportError:  # pragma: no cover
        s = source or ""
        return s if s.startswith(("ext:", "public:")) else "nask"


_LEVEL_CACHE: dict[tuple[str, str, str], str] = {}


def gene_level(family: str, kind: str, dep: str | None) -> str:
    key = (family, kind, dep or "")
    lvl = _LEVEL_CACHE.get(key)
    if lvl is None:
        try:
            from types import SimpleNamespace

            from hydra.synapse import level

            lvl = level(SimpleNamespace(dependency_group=dep, kind=kind, family=family))
        except Exception:  # noqa: BLE001 — same fallback as api_task._gene_level
            lvl = "unknown"
        _LEVEL_CACHE[key] = lvl
    return lvl


def edge_level(hypothesis: str, detail: dict[str, Any]) -> str:
    """hydra.api_graph._level; '' when the hypothesis is not a link level (independent)."""
    lvl = LEVEL_OF.get(hypothesis)
    if lvl in ("weak", "asset") and float(detail.get("kit_bits") or 0) >= KIT_BITS:
        return "kit"
    return lvl or ""


def dominant(bits: dict[str, float]) -> str | None:
    pos = {k: v for k, v in (bits or {}).items() if v and v > 0}
    return max(pos, key=pos.get) if pos else None  # type: ignore[arg-type]


def _best(counter: dict[str, float]) -> str | None:
    """Deterministic argmax (largest value, then name)."""
    items = [(v, k) for k, v in counter.items() if v and v > 0]
    return min(items, key=lambda t: (-t[0], t[1]))[1] if items else None


def solo(iid: str) -> str:
    return f"solo:{iid}"


def _chunks(ids: list[str], n: int = 900) -> Iterable[list[str]]:
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _q(ids: list[str]) -> str:
    return ",".join("?" * len(ids)) or "NULL"


# =============================================================================================== facts
# Row-level facts read from the v2 tables. Shared by the base build, the deltas and the reference recompute.


def base_scope(conn: sqlite3.Connection, run: str) -> list[str]:
    """Incidents of the immutable run: its campaign members and its own (self-)assignments; a run without either
    falls back to the ends of its pairs (as `api_task.run_incidents`, without later assign runs)."""
    ids = {r[0] for r in conn.execute("SELECT incident_id FROM campaign_members WHERE run_id=?", (run,))}
    ids |= {r[0] for r in conn.execute("SELECT incident_id FROM assignments WHERE run_id=?", (run,))}
    if not ids:
        ids = {x for r in conn.execute("SELECT a, b FROM pairs WHERE run_id=?", (run,)) for x in r}
    return sorted(ids)


def run_lineage(conn: sqlite3.Connection, run: str | None) -> list[str]:
    """The run and every analyse run it revises (`params.revision_of` chain of hydra.review, newest first).

    CR-E2E-5: an analyst review publishes a revision run; material assigned against the base run (or an earlier
    revision) after the analysis stays attached to every later revision of it, so one review never hides it."""
    out: list[str] = []
    cur = run
    while cur and cur not in out:
        out.append(cur)
        r = conn.execute("SELECT json_extract(params, '$.revision_of') FROM runs WHERE id=?", (cur,)).fetchone()
        cur = r[0] if r else None
    return out


def assign_runs(conn: sqlite3.Connection, run: str | None, kind: str = "assign") -> list[str]:
    """Runs of `kind` (assign / assign_batch) scored against the run or any run of its revision lineage."""
    lineage = run_lineage(conn, run)
    if not lineage:
        return []
    return [r[0] for r in conn.execute(
        f"SELECT id FROM runs WHERE kind=? AND json_extract(params, '$.base_run') IN ({_q(lineage)})"
        " ORDER BY created_at, id", (kind, *lineage))]


def lineage_heirs(conn: sqlite3.Connection, base: str | None) -> list[str]:
    """Materialised runs whose lineage contains `base` (the base itself and its revisions)."""
    if not base or not enabled():
        return []
    return [r for r in _ready_runs(conn) if base in run_lineage(conn, r)]


def latest_assignments(conn: sqlite3.Connection, run: str) -> dict[str, dict[str, Any]]:
    """Newest assignment per incident over the run and its assign runs (api_task._latest_assignments order)."""
    runs = [run, *assign_runs(conn, run)]
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        f"SELECT incident_id, run_id, decision, campaign_id, created_at FROM assignments WHERE run_id IN"
        f" ({_q(runs)}) ORDER BY created_at, run_id", runs):
        out[r[0]] = {"run_id": r[1], "decision": r[2], "campaign_id": r[3], "created_at": r[4]}
    return out


def incident_facts(conn: sqlite3.Connection, ids: list[str], *, thumbs: bool = False) -> dict[str, dict[str, Any]]:
    """Contribution of each incident to the counters: corpus, day, artifact roles, genes by family/status,
    enrichment-backed genes, distinct observed gene values (keys of `agg_genes`)."""
    facts: dict[str, dict[str, Any]] = {}
    for chunk in _chunks(ids):
        for r in conn.execute(f"SELECT id, source, title, observed_at FROM incidents WHERE id IN ({_q(chunk)})", chunk):
            obs = r[3] or None
            facts[r[0]] = {"source": r[1], "corpus": corpus_of(r[1]), "title": r[2], "observed_at": obs,
                           "day": obs[:10] if obs and DATE_RE.match(obs) else None, "roles": {}, "genes": {},
                           "enr": 0, "values": [], "thumb": None}
        for r in conn.execute(f"SELECT incident_id, role, COUNT(*) FROM incident_artifacts WHERE incident_id IN"
                              f" ({_q(chunk)}) GROUP BY 1, 2", chunk):
            if r[0] in facts:
                facts[r[0]]["roles"][r[1]] = r[2]
        for r in conn.execute(
            f"SELECT incident_id, family, status, COUNT(*), SUM(CASE WHEN evidence LIKE '%enrichment_key%' AND"
            f" json_extract(evidence, '$.enrichment_key') IS NOT NULL THEN 1 ELSE 0 END) FROM genes WHERE"
            f" incident_id IN ({_q(chunk)}) GROUP BY 1, 2, 3", chunk):
            if r[0] in facts:
                facts[r[0]]["genes"][f"{r[1]}|{r[2]}"] = r[3]
                facts[r[0]]["enr"] += int(r[4] or 0)
        for r in conn.execute(
            f"SELECT DISTINCT incident_id, family, kind, COALESCE(dependency_group, ''), value FROM genes WHERE"
            f" status='observed' AND value IS NOT NULL AND vector_ref IS NULL AND incident_id IN ({_q(chunk)})",
            chunk):
            if r[0] in facts:
                facts[r[0]]["values"].append(UNIT.join((r[1], r[2], r[3], r[4])))
        if thumbs:
            best: dict[str, tuple[int, int, str]] = {}
            for r in conn.execute(
                "SELECT ia.incident_id, ia.artifact_id, ia.role, a.size FROM incident_artifacts ia JOIN artifacts a"
                f" ON a.id=ia.artifact_id WHERE ia.incident_id IN ({_q(chunk)}) AND a.mime LIKE 'image/%' AND"
                " a.mime!='image/svg+xml'", chunk):
                k = (THUMB_ORDER.get(r[2], 3), -(r[3] or 0), r[1])
                if r[0] not in best or k < best[r[0]]:
                    best[r[0]] = k
            for iid, k in best.items():
                if iid in facts:
                    facts[iid]["thumb"] = k[2]
    for f in facts.values():
        f["values"].sort()
    return facts


def pair_fact(a: str, b: str, total_bits: float, probability: float, hypothesis: str, detail: dict[str, Any],
              src_run: str) -> dict[str, Any]:
    fam = detail.get("family_bits") or {}
    return {"a": a, "b": b, "bits": round(float(total_bits), 3), "p": probability, "hypothesis": hypothesis,
            "level": edge_level(hypothesis, detail), "family": dominant(fam),
            "family_bits": {k: round(float(v), 2) for k, v in fam.items() if v}, "src_run": src_run}


def run_pairs(conn: sqlite3.Connection, run: str, involving: str | None = None) -> list[dict[str, Any]]:
    """Non-independent pairs of a run (optionally only those of one incident)."""
    sql = ("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=?"
           " AND hypothesis!='independent'")
    args: list[Any] = [run]
    if involving is not None:
        sql += " AND (a=? OR b=?)"
        args += [involving, involving]
    return [pair_fact(r[0], r[1], r[2], r[3], r[4], _loads(r[5]), run) for r in conn.execute(sql, args)]


def one_pair(conn: sqlite3.Connection, run: str, a: str, b: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=? AND a=?"
                     " AND b=?", (run, a, b)).fetchone()
    return pair_fact(r[0], r[1], r[2], r[3], r[4], _loads(r[5]), run) if r is not None else None


def campaign_facts(conn: sqlite3.Connection, run: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]],
                                                                 dict[str, str]]:
    """(campaigns {id: label, group_id, first_seen, last_seen, confidence}, groups [{id, campaign_ids}], members)."""
    from hydra.api_task import _groups

    camps: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT id, label, confidence, detail FROM campaigns WHERE run_id=?", (run,)):
        d = _loads(r[3])
        camps[r[0]] = {"label": r[1], "confidence": r[2], "group_id": d.get("group_id"),
                       "first_seen": d.get("first_seen"), "last_seen": d.get("last_seen")}
    groups = [{"id": g["id"], "campaign_ids": list(g.get("campaign_ids") or [])} for g in _groups(conn, run)]
    group_of = {cid: g["id"] for g in groups for cid in g["campaign_ids"]}
    for cid, c in camps.items():
        c["group_id"] = c["group_id"] or group_of.get(cid)
    members = {r[0]: r[1] for r in conn.execute(
        "SELECT incident_id, campaign_id FROM campaign_members WHERE run_id=?", (run,))}
    return camps, groups, members


def pair_reviews(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Effective analyst state per pair (must_link / cannot_link / unsure), as the graph overlay shows it."""
    from hydra.api_task import _pair_reviews

    return {k: REVIEW_OF[v] for k, v in _pair_reviews(conn).items() if v in REVIEW_OF}


def export_counts(conn: sqlite3.Connection, run: str) -> Counter:
    return Counter(r[0] or "unknown" for r in conn.execute(
        "SELECT json_extract(payload, '$.kind') FROM events WHERE type=? AND json_extract(payload, '$.run_id')=?",
        (EXPORT_EVENT, run)))


def map_campaign(iid: str, decision: str | None, campaign_id: str | None, camps: dict[str, Any]) -> str:
    """Where new material sits on the campaign map: the campaign it was assigned to, else its own solo disc."""
    if decision == "assign" and campaign_id and campaign_id in camps:
        return campaign_id
    return solo(iid)


# =============================================================================================== aggregation
# One pure function from a full description of the run state to the rows of every agg table. Used for the base
# install and by the reference recompute.


def incident_counts(f: dict[str, Any], is_new: bool, decision: str | None, decision_new: bool) -> Counter:
    """(dim, key) -> n, and ("~new", dim, key) -> n_new for one incident."""
    c: Counter = Counter()

    def add(dim: str, key: str, n: int = 1, new: bool = is_new) -> None:
        if n:
            c[(dim, key)] += n
            if new:
                c[("~new", dim, key)] += n

    add("incidents", "*")
    add("source", f["source"] or "")
    add("corpus", f["corpus"])
    if f["day"]:
        add("day", f["day"])
    for role, n in f["roles"].items():
        add("role", role, n)
    for key, n in f["genes"].items():
        add("gene", key, n)
        fam, _, st = key.partition("|")
        if st == "observed":
            add("family", fam, n)
    if f["enr"]:
        add("enriched", "*")
        add("enr_genes", "*", f["enr"])
    if decision:
        add("decision", decision, 1, decision_new)
    return c


def pair_counts(p: dict[str, Any]) -> Counter:
    c: Counter = Counter()
    for dim, key in (("level", p["level"] or "-"), *((("review", p["review"]),) if p.get("review") else ())):
        c[(dim, key)] += 1
        if p.get("is_new"):
            c[("~new", dim, key)] += 1
    return c


def campaign_row(cid: str, info: dict[str, Any] | None, members: list[tuple[str | None, str | None, bool]],
                 internal: list[dict[str, Any]]) -> dict[str, Any]:
    """`members`: (day, title, is_new). `internal`: pairs with both ends in the campaign. `_fam` (positive family
    bits of the internal links) is kept in `meta` for the O(1) path of `_campaign_add`."""
    is_solo = info is None
    days_new = [d for d, _t, new in members if new and d]
    if is_solo:
        first = last = members[0][0] if members else None
        label = (members[0][1] if members else None) or cid
        group = None
    else:
        first, last = info.get("first_seen"), info.get("last_seen")
        label, group = info.get("label"), info.get("group_id")
        for d in days_new:
            if first is None or d < str(first)[:10]:
                first = d
            if last is None or d > str(last)[:10]:
                last = d
    fam: dict[str, float] = defaultdict(float)
    for p in internal:
        for k, v in p["family_bits"].items():
            if v > 0:
                fam[k] += v
    return {"campaign_id": cid, "label": label, "group_id": group, "solo": int(is_solo), "size": len(members),
            "n_new": sum(1 for *_, new in members if new), "first_seen": first, "last_seen": last,
            "internal_edges": len(internal),
            # as the overview (api_graph._overview_payload): maxima start at 0.0
            "max_internal_bits": round(max(0.0, *(p["bits"] for p in internal)), 3) if internal else None,
            "family": _best(fam), "_fam": {k: round(v, 6) for k, v in sorted(fam.items())}}


def link_rows(ca: str, cb: str, pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cross-campaign link per level (analyst cannot-links and level-less pairs never join campaigns)."""
    by: dict[str, dict[str, Any]] = {}
    for p in pairs:
        if p.get("review") == "cannot_link" or not p["level"]:
            continue
        cur = by.setdefault(p["level"], {"ca": ca, "cb": cb, "level": p["level"], "pairs": 0, "max_bits": 0.0,
                                         "sum_bits": 0.0, "family_bits": defaultdict(float), "n_new": 0})
        cur["pairs"] += 1
        cur["max_bits"] = max(cur["max_bits"], p["bits"])
        cur["sum_bits"] += p["bits"]
        if p["family"]:
            cur["family_bits"][p["family"]] += p["bits"]
        cur["n_new"] += int(bool(p.get("is_new")))
    for cur in by.values():
        cur["sum_bits"] = round(cur["sum_bits"], 6)
        cur["family_bits"] = {k: round(v, 6) for k, v in sorted(cur["family_bits"].items())}
    return [by[k] for k in sorted(by)]


def gene_transition(df0: int, dn0: int, df1: int, dn1: int) -> tuple[int, int]:
    """Change of the distinct-value counter of a tier when one value goes from (df0, dn0) to (df1, dn1).
    A value is new when only new material carries it (df == df_new > 0)."""
    p0, p1 = df0 > 0, df1 > 0
    n0, n1 = p0 and dn0 == df0, p1 and dn1 == df1
    return int(p1) - int(p0), int(n1) - int(n0)


def aggregate(state: dict[str, Any]) -> dict[str, Any]:
    """Rows of every agg table for a full run state:

    state = {"incidents": {iid: {"facts", "is_new", "campaign", "decision", "decision_new", "assign_run"}},
             "campaigns": {cid: info}, "groups": [{id, campaign_ids}], "pairs": {(a, b): pair fact + review/is_new},
             "exports": Counter(kind)}
    """
    counts: Counter = Counter()
    df: Counter = Counter()
    dfn: Counter = Counter()
    for inc in state["incidents"].values():
        counts += incident_counts(inc["facts"], inc["is_new"], inc["decision"], inc["decision_new"])
        for v in inc["facts"]["values"]:
            df[v] += 1
            if inc["is_new"]:
                dfn[v] += 1
    genes = {}
    for v, n in df.items():
        fam, kind, dep, _ = v.split(UNIT, 3)
        lvl = gene_level(fam, kind, dep or None)
        genes[v] = {"gkey": v, "level": lvl, "df": n, "df_new": dfn[v]}
        dn, dnew = gene_transition(0, 0, n, dfn[v])
        counts[("tier", lvl)] += dn
        if dnew:
            counts[("~new", "tier", lvl)] += dnew
    camp_of = {iid: inc["campaign"] for iid, inc in state["incidents"].items()}
    pairs = {}
    by_link: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (a, b), p in state["pairs"].items():
        ca, cb = sorted((camp_of.get(a) or solo(a), camp_of.get(b) or solo(b)))
        row = {**p, "ca": ca, "cb": cb}
        pairs[(a, b)] = row
        by_link[(ca, cb)].append(row)
        counts += pair_counts(row)
    members: dict[str, list[tuple[dict[str, Any], bool]]] = defaultdict(list)
    for iid in sorted(state["incidents"]):
        inc = state["incidents"][iid]
        members[inc["campaign"]].append((inc["facts"]["day"], inc["facts"]["title"], inc["is_new"]))
    camps = {}
    for cid in set(state["campaigns"]) | set(members):
        info = state["campaigns"].get(cid)
        if info is None and not members.get(cid):
            continue
        camps[cid] = campaign_row(cid, info, members.get(cid, []), by_link.get((cid, cid), []))
    links = {}
    for (ca, cb), ps in by_link.items():
        if ca != cb:
            for lr in link_rows(ca, cb, ps):
                links[(ca, cb, lr["level"])] = lr
    counts[("campaigns", "*")] = len(state["campaigns"])
    counts[("campaigns", "multi")] = sum(1 for c in camps.values() if not c["solo"] and c["size"] > 1)
    counts[("groups", "*")] = len(state["groups"])
    for g in state["groups"]:
        cs = [c for c in camps.values() if not c["solo"] and c["group_id"] == g["id"]]
        counts[("group", g["id"])] = sum(c["size"] for c in cs)
        counts[("~new", "group", g["id"])] = sum(c["n_new"] for c in cs)
    for kind, n in state["exports"].items():
        counts[("export", kind)] += n
        counts[("~new", "export", kind)] += n
    out_counts = {}
    for k, n in counts.items():
        if k[0] == "~new":
            continue
        out_counts[k] = [n, counts.get(("~new", *k), 0)]
    for k, n in counts.items():  # n_new without a total (cannot happen, but never drop data silently)
        if k[0] == "~new" and k[1:] not in out_counts and n:
            out_counts[k[1:]] = [0, n]
    keep_zero = {("campaigns", "*"), ("campaigns", "multi"), ("groups", "*")}
    keep_zero |= {("group", g["id"]) for g in state["groups"]}
    out_counts = {k: v for k, v in out_counts.items() if v != [0, 0] or k in keep_zero}
    incidents = {iid: {"is_new": int(inc["is_new"]), "campaign_id": inc["campaign"], "decision": inc["decision"],
                       "assign_run": inc["assign_run"]} for iid, inc in state["incidents"].items()}
    return {"counts": out_counts, "genes": genes, "pairs": pairs, "campaigns": camps, "links": links,
            "incidents": incidents}


# =============================================================================================== base + reference


def base_state(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Immutable part of a run: its incidents with their own decisions, pairs (no review state), campaigns."""
    scope = base_scope(conn, run)
    camps, groups, members = campaign_facts(conn, run)
    own = {r[0]: r[1] for r in conn.execute("SELECT incident_id, decision FROM assignments WHERE run_id=?"
                                             " ORDER BY created_at", (run,))}
    facts = incident_facts(conn, scope)
    incidents = {iid: {"facts": facts[iid], "is_new": False, "campaign": members.get(iid) or solo(iid),
                       "decision": own.get(iid), "decision_new": False, "assign_run": None}
                 for iid in scope if iid in facts}
    pairs = {(p["a"], p["b"]): {**p, "review": None, "is_new": 0} for p in run_pairs(conn, run)}
    return {"incidents": incidents, "campaigns": camps, "groups": groups, "pairs": pairs, "exports": Counter()}


def recompute(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Reference: every aggregate of the run from scratch (base + new material + reviews + exports)."""
    st = base_state(conn, run)
    latest = latest_assignments(conn, run)
    new_ids = sorted(i for i, a in latest.items() if i not in st["incidents"] and a["run_id"] != run)
    facts = incident_facts(conn, new_ids, thumbs=True)
    for iid, inc in st["incidents"].items():
        a = latest.get(iid)
        if a is not None and a["run_id"] != run:
            inc.update(decision=a["decision"], decision_new=True, assign_run=a["run_id"])
    for iid in new_ids:
        if iid not in facts:
            continue
        a = latest[iid]
        st["incidents"][iid] = {"facts": facts[iid], "is_new": True,
                                "campaign": map_campaign(iid, a["decision"], a["campaign_id"], st["campaigns"]),
                                "decision": a["decision"], "decision_new": True, "assign_run": a["run_id"]}
        for p in run_pairs(conn, a["run_id"], involving=iid):
            st["pairs"].setdefault((p["a"], p["b"]), {**p, "review": None, "is_new": 1})
    reviews = pair_reviews(conn)
    for (a, b), rv in reviews.items():
        p = st["pairs"].get((a, b))
        if p is not None:
            p["review"] = rv
        elif rv == "must_link":
            extra = _accepted_pair(conn, run, a, b, st["incidents"])
            if extra is not None:
                st["pairs"][(a, b)] = extra
    st["exports"] = export_counts(conn, run)
    return aggregate(st)


def _accepted_pair(conn: sqlite3.Connection, run: str, a: str, b: str, incidents: dict[str, Any]
                   ) -> dict[str, Any] | None:
    """An analyst must-link on a pair the scorer left independent is still a link (level `review`), read from the
    base run, or from the assign run of a new incident at one end."""
    srcs = [(run, 0)] + [(incidents[x]["assign_run"], 1) for x in (a, b)
                         if x in incidents and incidents[x]["is_new"] and incidents[x]["assign_run"]]
    for src, new in srcs:
        p = one_pair(conn, src, a, b)
        if p is not None:
            p["level"] = p["level"] or "review"
            return {**p, "review": "must_link", "is_new": new}
    return None


# =============================================================================================== table io


def _clear(conn: sqlite3.Connection, run: str) -> None:
    for t in AGG_TABLES:
        conn.execute(f"DELETE FROM {t} WHERE run_id=?", (run,))


def _write(conn: sqlite3.Connection, run: str, rows: dict[str, Any], facts: dict[str, dict[str, Any]],
           at: dict[str, Any]) -> None:
    conn.executemany("INSERT INTO agg_counts (run_id, dim, key, n, n_new) VALUES (?, ?, ?, ?, ?)",
                     [(run, d, k, v[0], v[1]) for (d, k), v in rows["counts"].items()])
    conn.executemany("INSERT INTO agg_genes (run_id, gkey, level, df, df_new) VALUES (?, ?, ?, ?, ?)",
                     [(run, g["gkey"], g["level"], g["df"], g["df_new"]) for g in rows["genes"].values()])
    conn.executemany(
        "INSERT INTO agg_pairs (run_id, a, b, ca, cb, bits, p, hypothesis, level, family, family_bits, review,"
        " is_new, src_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run, p["a"], p["b"], p["ca"], p["cb"], p["bits"], p["p"], p["hypothesis"], p["level"], p["family"],
          _dumps(p["family_bits"]), p.get("review"), int(p.get("is_new") or 0), p["src_run"])
         for p in rows["pairs"].values()])
    _put_campaigns(conn, run, rows["campaigns"].values())
    conn.executemany(
        "INSERT INTO agg_links (run_id, ca, cb, level, pairs, max_bits, sum_bits, family_bits, n_new)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run, x["ca"], x["cb"], x["level"], x["pairs"], x["max_bits"], x["sum_bits"], _dumps(x["family_bits"]),
          x["n_new"]) for x in rows["links"].values()])
    conn.executemany(
        "INSERT INTO agg_incidents (run_id, incident_id, is_new, campaign_id, decision, assign_run, seq, data)"
        " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        [(run, iid, i["is_new"], i["campaign_id"], i["decision"], i["assign_run"],
          _dumps({**facts[iid], "at": at.get(iid)})) for iid, i in rows["incidents"].items()])


def _put_campaigns(conn: sqlite3.Connection, run: str, rows: Iterable[dict[str, Any]], info: dict | None = None
                   ) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO agg_campaigns (run_id, campaign_id, label, group_id, solo, size, n_new, first_seen,"
        " last_seen, internal_edges, max_internal_bits, family, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run, c["campaign_id"], c["label"], c["group_id"], c["solo"], c["size"], c["n_new"], c["first_seen"],
          c["last_seen"], c["internal_edges"], c["max_internal_bits"], c["family"],
          _dumps({**(c.get("meta") or {}), "fam": c.get("_fam") or {}})) for c in rows])


def snapshot(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Materialised state in the shape of `aggregate()` (for tests and `verify`)."""
    out: dict[str, Any] = {"counts": {}, "genes": {}, "pairs": {}, "campaigns": {}, "links": {}, "incidents": {}}
    for r in conn.execute("SELECT dim, key, n, n_new FROM agg_counts WHERE run_id=?", (run,)):
        out["counts"][(r[0], r[1])] = [r[2], r[3]]
    for r in conn.execute("SELECT gkey, level, df, df_new FROM agg_genes WHERE run_id=?", (run,)):
        out["genes"][r[0]] = {"gkey": r[0], "level": r[1], "df": r[2], "df_new": r[3]}
    for r in conn.execute("SELECT a, b, ca, cb, bits, p, hypothesis, level, family, family_bits, review, is_new,"
                          " src_run FROM agg_pairs WHERE run_id=?", (run,)):
        out["pairs"][(r[0], r[1])] = {"a": r[0], "b": r[1], "ca": r[2], "cb": r[3], "bits": r[4], "p": r[5],
                                      "hypothesis": r[6], "level": r[7], "family": r[8],
                                      "family_bits": _loads(r[9]), "review": r[10], "is_new": r[11],
                                      "src_run": r[12]}
    for r in conn.execute("SELECT campaign_id, label, group_id, solo, size, n_new, first_seen, last_seen,"
                          " internal_edges, max_internal_bits, family FROM agg_campaigns WHERE run_id=?", (run,)):
        out["campaigns"][r[0]] = {"campaign_id": r[0], "label": r[1], "group_id": r[2], "solo": r[3], "size": r[4],
                                  "n_new": r[5], "first_seen": r[6], "last_seen": r[7], "internal_edges": r[8],
                                  "max_internal_bits": r[9], "family": r[10]}
    for r in conn.execute("SELECT ca, cb, level, pairs, max_bits, sum_bits, family_bits, n_new FROM agg_links"
                          " WHERE run_id=?", (run,)):
        out["links"][(r[0], r[1], r[2])] = {"ca": r[0], "cb": r[1], "level": r[2], "pairs": r[3], "max_bits": r[4],
                                            "sum_bits": r[5], "family_bits": _loads(r[6]), "n_new": r[7]}
    for r in conn.execute("SELECT incident_id, is_new, campaign_id, decision, assign_run FROM agg_incidents"
                          " WHERE run_id=?", (run,)):
        out["incidents"][r[0]] = {"is_new": r[1], "campaign_id": r[2], "decision": r[3], "assign_run": r[4]}
    return out


def normalise(snap: dict[str, Any]) -> dict[str, Any]:
    """Comparable form: floats rounded (sums of rounded bits differ in the last ulp by summation order)."""

    def fix(v: Any) -> Any:
        if isinstance(v, float):
            return round(v, 4)
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [fix(x) for x in v]
        return v

    def strip(v: Any) -> Any:
        return {k: x for k, x in v.items() if not k.startswith("_") and k != "meta"} if isinstance(v, dict) else v

    return {name: {k: fix(strip(v)) for k, v in rows.items()} for name, rows in snap.items()}


# =============================================================================================== state


def state_row(conn: sqlite3.Connection, run: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT run_id, status, version, seq, base_seq, base_finished_at, built_at, updated_at"
                     " FROM agg_state WHERE run_id=?", (run,)).fetchone()
    if r is None:
        return None
    return {"run_id": r[0], "status": r[1], "version": r[2], "seq": r[3], "base_seq": r[4],
            "base_finished_at": r[5], "built_at": r[6], "updated_at": r[7]}


def is_ready(conn: sqlite3.Connection, run: str | None) -> bool:
    if not run or not enabled():
        return False
    try:
        r = conn.execute("SELECT status FROM agg_state WHERE run_id=?", (run,)).fetchone()
    except sqlite3.OperationalError:  # schema older than v5 (read-only copy)
        return False
    return r is not None and r[0] == "ready"


def _max_seq(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return int(r[0]) if r else 0


# =============================================================================================== deltas


@dataclass
class Change:
    kinds: set[str] = field(default_factory=set)
    counts: Counter = field(default_factory=Counter)        # (dim, key) -> dn ; ("~new", dim, key) -> dn_new
    campaigns: set[str] = field(default_factory=set)
    links: set[tuple[str, str]] = field(default_factory=set)
    nodes: set[str] = field(default_factory=set)
    removed_nodes: set[str] = field(default_factory=set)
    edges: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    genes: bool = False
    exports: bool = False
    batch: dict[str, Any] | None = None  # merge-batch4: an assign_batch run against this base (hydra.batch)

    def __bool__(self) -> bool:
        return bool(self.counts or self.campaigns or self.links or self.nodes or self.removed_nodes or self.genes
                    or self.exports or self.batch)


def _apply_counts(conn: sqlite3.Connection, run: str, ch: Change, c: Counter, sign: int = 1) -> None:
    for k, n in c.items():
        if n:
            ch.counts[k] += sign * n


def _flush_counts(conn: sqlite3.Connection, run: str, ch: Change) -> set[tuple[str, str]]:
    keys = {k[1:] if k[0] == "~new" else k for k, v in ch.counts.items() if v}
    for dim, key in keys:
        dn, dnew = ch.counts.get((dim, key), 0), ch.counts.get(("~new", dim, key), 0)
        conn.execute("INSERT INTO agg_counts (run_id, dim, key, n, n_new) VALUES (?, ?, ?, ?, ?) ON CONFLICT"
                     " (run_id, dim, key) DO UPDATE SET n = n + excluded.n, n_new = n_new + excluded.n_new",
                     (run, dim, key, dn, dnew))
    conn.executemany("DELETE FROM agg_counts WHERE run_id=? AND dim=? AND key=? AND n=0 AND n_new=0",
                     [(run, d, k) for d, k in keys if d not in ("campaigns", "groups", "group")])
    ch.counts.clear()
    return keys


def _genes_delta(conn: sqlite3.Connection, run: str, ch: Change, values: list[str], sign: int, is_new: bool) -> None:
    if not values:
        return
    cur: dict[str, tuple[int, int, str]] = {}
    for chunk in _chunks(values):
        for r in conn.execute(f"SELECT gkey, df, df_new, level FROM agg_genes WHERE run_id=? AND gkey IN"
                              f" ({_q(chunk)})", [run, *chunk]):
            cur[r[0]] = (r[1], r[2], r[3])
    upd, ins, dele = [], [], []
    for v in values:
        df0, dn0, lvl = cur.get(v, (0, 0, None))
        if lvl is None:
            fam, kind, dep, _ = v.split(UNIT, 3)
            lvl = gene_level(fam, kind, dep or None)
        df1, dn1 = df0 + sign, dn0 + (sign if is_new else 0)
        dn, dnew = gene_transition(df0, dn0, df1, dn1)
        if dn:
            ch.counts[("tier", lvl)] += dn
        if dnew:
            ch.counts[("~new", "tier", lvl)] += dnew
        if df1 <= 0:
            dele.append((run, v))
        elif v in cur:
            upd.append((df1, dn1, run, v))
        else:
            ins.append((run, v, lvl, df1, dn1))
    conn.executemany("UPDATE agg_genes SET df=?, df_new=? WHERE run_id=? AND gkey=?", upd)
    conn.executemany("INSERT INTO agg_genes (run_id, gkey, level, df, df_new) VALUES (?, ?, ?, ?, ?)", ins)
    conn.executemany("DELETE FROM agg_genes WHERE run_id=? AND gkey=?", dele)
    ch.genes = True


def _pairs_of(conn: sqlite3.Connection, run: str, iid: str, src_run: str) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT a, b, ca, cb, bits, level, review, is_new FROM agg_pairs WHERE run_id=? AND"
                          " src_run=? AND (a=? OR b=?)", (run, src_run, iid, iid)):
        out.append({"a": r[0], "b": r[1], "ca": r[2], "cb": r[3], "bits": r[4], "level": r[5], "review": r[6],
                    "is_new": r[7]})
    return out


def _campaign_info(conn: sqlite3.Connection, run: str, cid: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT meta FROM agg_campaigns WHERE run_id=? AND campaign_id=? AND solo=0",
                     (run, cid)).fetchone()
    return _loads(r[0]).get("base") if r is not None else None


def _refresh_campaigns(conn: sqlite3.Connection, run: str, ch: Change, cids: set[str]) -> None:
    """Local recompute of the touched campaign discs (size, new members, first/last seen, internal links)."""
    for cid in cids:
        info = _campaign_info(conn, run, cid)
        before = conn.execute("SELECT size, solo FROM agg_campaigns WHERE run_id=? AND campaign_id=?",
                              (run, cid)).fetchone()
        members = [(r[0], r[1], bool(r[2])) for r in conn.execute(
            "SELECT json_extract(data, '$.day'), json_extract(data, '$.title'), is_new FROM agg_incidents WHERE"
            " run_id=? AND campaign_id=? ORDER BY incident_id", (run, cid))]
        internal = [{"bits": r[0], "family_bits": _loads(r[1])} for r in conn.execute(
            "SELECT bits, family_bits FROM agg_pairs WHERE run_id=? AND ca=? AND cb=?", (run, cid, cid))]
        if info is None and not members:
            conn.execute("DELETE FROM agg_campaigns WHERE run_id=? AND campaign_id=?", (run, cid))
            after_multi = False
        else:
            row = campaign_row(cid, info, members, internal)
            if info is not None:
                row["meta"] = {"base": info}
            _put_campaigns(conn, run, [row])
            after_multi = not row["solo"] and row["size"] > 1
        before_multi = before is not None and not before[1] and before[0] > 1
        if after_multi != before_multi:
            ch.counts[("campaigns", "multi")] += 1 if after_multi else -1
        ch.campaigns.add(cid)
    groups: set[str] = set()
    for cid in cids:
        r = conn.execute("SELECT group_id FROM agg_campaigns WHERE run_id=? AND campaign_id=?", (run, cid)).fetchone()
        if r is not None and r[0]:
            groups.add(r[0])
    for gid in groups:
        cur = conn.execute("SELECT n, n_new FROM agg_counts WHERE run_id=? AND dim='group' AND key=?",
                           (run, gid)).fetchone()
        tot = conn.execute("SELECT COALESCE(SUM(size), 0), COALESCE(SUM(n_new), 0) FROM agg_campaigns WHERE"
                           " run_id=? AND group_id=? AND solo=0", (run, gid)).fetchone()
        if cur is None:  # group not part of the base (cannot happen for base campaigns)
            continue
        # counters are flushed once per change: include what this change already added for the group
        pend_n, pend_new = ch.counts.get(("group", gid), 0), ch.counts.get(("~new", "group", gid), 0)
        ch.counts[("group", gid)] += tot[0] - (cur[0] + pend_n)
        ch.counts[("~new", "group", gid)] += tot[1] - (cur[1] + pend_new)


def _refresh_links(conn: sqlite3.Connection, run: str, ch: Change, keys: set[tuple[str, str]]) -> None:
    for ca, cb in keys:
        if ca == cb:
            continue
        pairs = [{"bits": r[0], "level": r[1], "family": r[2], "review": r[3], "is_new": r[4]} for r in conn.execute(
            "SELECT bits, level, family, review, is_new FROM agg_pairs WHERE run_id=? AND ca=? AND cb=?",
            (run, ca, cb))]
        conn.execute("DELETE FROM agg_links WHERE run_id=? AND ca=? AND cb=?", (run, ca, cb))
        conn.executemany(
            "INSERT INTO agg_links (run_id, ca, cb, level, pairs, max_bits, sum_bits, family_bits, n_new)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(run, x["ca"], x["cb"], x["level"], x["pairs"], x["max_bits"], x["sum_bits"], _dumps(x["family_bits"]),
              x["n_new"]) for x in link_rows(ca, cb, pairs)])
        ch.links.add((ca, cb))


def _insert_pairs(conn: sqlite3.Connection, run: str, ch: Change, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO agg_pairs (run_id, a, b, ca, cb, bits, p, hypothesis, level, family, family_bits,"
        " review, is_new, src_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(run, p["a"], p["b"], p["ca"], p["cb"], p["bits"], p["p"], p["hypothesis"], p["level"], p["family"],
          _dumps(p["family_bits"]), p.get("review"), int(p.get("is_new") or 0), p["src_run"]) for p in rows])
    for p in rows:
        _apply_counts(conn, run, ch, pair_counts(p))
        ch.links.add((p["ca"], p["cb"]))
        if p.get("is_new"):
            ch.edges[(p["a"], p["b"])] = p


def _camp_lookup(conn: sqlite3.Connection, run: str) -> Any:
    cache: dict[str, str] = {}

    def of(iid: str) -> str:
        if iid not in cache:
            r = conn.execute("SELECT campaign_id FROM agg_incidents WHERE run_id=? AND incident_id=?",
                             (run, iid)).fetchone()
            cache[iid] = r[0] if r is not None else solo(iid)
        return cache[iid]

    return of


def _campaign_add(conn: sqlite3.Connection, run: str, ch: Change, cid: str, facts: dict[str, Any],
                  internal: list[dict[str, Any]]) -> None:
    """O(1) update of a campaign disc when one new member (and its internal links) arrives (insert-only: sizes,
    first/last seen and internal maxima only grow)."""
    r = conn.execute("SELECT label, group_id, solo, size, n_new, first_seen, last_seen, internal_edges,"
                     " max_internal_bits, meta FROM agg_campaigns WHERE run_id=? AND campaign_id=?",
                     (run, cid)).fetchone()
    ch.campaigns.add(cid)
    if r is None:  # the solo disc of the new incident itself
        _put_campaigns(conn, run, [campaign_row(cid, None, [(facts["day"], facts["title"], True)], internal)])
        return
    meta = _loads(r[9])
    fam: dict[str, float] = defaultdict(float, meta.get("fam") or {})
    first, last = r[5], r[6]
    d = facts["day"]
    if d and not r[2]:
        if first is None or d < str(first)[:10]:
            first = d
        if last is None or d > str(last)[:10]:
            last = d
    best = r[8]
    for p in internal:
        best = max(0.0 if best is None else best, p["bits"])
        for k, v in p["family_bits"].items():
            if v > 0:
                fam[k] += v
    meta["fam"] = {k: round(v, 6) for k, v in sorted(fam.items())}
    size = r[3] + 1
    conn.execute("UPDATE agg_campaigns SET size=?, n_new=n_new+1, first_seen=?, last_seen=?, internal_edges=?,"
                 " max_internal_bits=?, family=?, meta=? WHERE run_id=? AND campaign_id=?",
                 (size, first, last, r[7] + len(internal), round(best, 3) if best is not None else None, _best(fam),
                  _dumps(meta), run, cid))
    if not r[2] and r[3] <= 1 < size:
        ch.counts[("campaigns", "multi")] += 1
    if r[1] and not r[2]:
        ch.counts[("group", r[1])] += 1
        ch.counts[("~new", "group", r[1])] += 1


def _link_add(conn: sqlite3.Connection, run: str, ch: Change, p: dict[str, Any]) -> None:
    """O(1) update of a campaign-to-campaign link by one new pair (insert-only: count, sum, max, family bits)."""
    if p["ca"] == p["cb"] or not p["level"] or p.get("review") == "cannot_link":
        return
    key = (run, p["ca"], p["cb"], p["level"])
    r = conn.execute("SELECT pairs, max_bits, sum_bits, family_bits, n_new FROM agg_links WHERE run_id=? AND ca=?"
                     " AND cb=? AND level=?", key).fetchone()
    fam: dict[str, float] = defaultdict(float, _loads(r[3]) if r else {})
    if p["family"]:
        fam[p["family"]] += p["bits"]
    new = int(bool(p.get("is_new")))
    vals = (1, max(0.0, p["bits"]), p["bits"], new) if r is None else (r[0] + 1, max(r[1], p["bits"]), r[2] + p["bits"],
                                                             r[4] + new)
    conn.execute("INSERT OR REPLACE INTO agg_links (run_id, ca, cb, level, pairs, max_bits, sum_bits, family_bits,"
                 " n_new) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (*key, vals[0], vals[1], round(vals[2], 6), _dumps({k: round(v, 6) for k, v in sorted(fam.items())}),
                  vals[3]))
    ch.links.add((p["ca"], p["cb"]))


def apply_assignment(conn: sqlite3.Connection, run: str, iid: str, a: dict[str, Any], seq: int,
                     reviews: dict[tuple[str, str], str] | None, ch: Change) -> None:
    """New decision for one incident from an assign run of `run` (delta; latest assignment wins)."""
    cur = conn.execute("SELECT is_new, campaign_id, decision, assign_run, data FROM agg_incidents WHERE run_id=?"
                       " AND incident_id=?", (run, iid)).fetchone()
    at = [a["created_at"], a["run_id"]]
    if cur is not None:
        data = _loads(cur[4])
        if data.get("at") and list(data["at"]) >= at:
            return  # an older decision arriving late never replaces a newer one
    ch.kinds.add("assign")
    if cur is not None and not cur[0]:  # a base incident decided again: only the decision changes
        if cur[2]:
            ch.counts[("decision", cur[2])] -= 1
            if cur[3]:
                ch.counts[("~new", "decision", cur[2])] -= 1
        ch.counts[("decision", a["decision"])] += 1
        ch.counts[("~new", "decision", a["decision"])] += 1
        data["at"] = at
        conn.execute("UPDATE agg_incidents SET decision=?, assign_run=?, seq=?, data=? WHERE run_id=? AND"
                     " incident_id=?", (a["decision"], a["run_id"], seq, _dumps(data), run, iid))
        return
    touched_c: set[str] = set()
    olds: list[dict[str, Any]] = []
    if cur is not None:  # new material decided again: take its old contribution out first
        old = _loads(cur[4])
        _apply_counts(conn, run, ch, incident_counts(old, True, cur[2], True), -1)
        _genes_delta(conn, run, ch, old.get("values") or [], -1, True)
        olds = _pairs_of(conn, run, iid, cur[3]) if cur[3] else []
        for p in olds:
            _apply_counts(conn, run, ch, pair_counts(p), -1)
            ch.links.add((p["ca"], p["cb"]))
            ch.edges.pop((p["a"], p["b"]), None)
        if olds:
            conn.execute("DELETE FROM agg_pairs WHERE run_id=? AND src_run=? AND (a=? OR b=?)", (run, cur[3], iid, iid))
        touched_c.add(cur[1])
    facts = incident_facts(conn, [iid], thumbs=True).get(iid)
    if facts is None:  # assignment of an unknown incident: nothing to count
        conn.execute("DELETE FROM agg_incidents WHERE run_id=? AND incident_id=?", (run, iid))
        ch.removed_nodes.add(iid)
        _refresh_campaigns(conn, run, ch, touched_c)
        _refresh_links(conn, run, ch, {(p["ca"], p["cb"]) for p in olds if p["ca"] != p["cb"]})
        return
    base_camps = {r[0]: True for r in conn.execute("SELECT campaign_id FROM agg_campaigns WHERE run_id=? AND solo=0",
                                                   (run,))}
    mc = map_campaign(iid, a["decision"], a["campaign_id"], base_camps)
    facts["at"] = at
    conn.execute("INSERT OR REPLACE INTO agg_incidents (run_id, incident_id, is_new, campaign_id, decision,"
                 " assign_run, seq, data) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                 (run, iid, mc, a["decision"], a["run_id"], seq, _dumps(facts)))
    _apply_counts(conn, run, ch, incident_counts(facts, True, a["decision"], True))
    _genes_delta(conn, run, ch, facts["values"], +1, True)
    of = _camp_lookup(conn, run)
    rows = []
    have = set()
    for p in run_pairs(conn, a["run_id"], involving=iid):
        ca, cb = sorted((of(p["a"]), of(p["b"])))
        rv = (reviews or {}).get((p["a"], p["b"]))
        rows.append({**p, "ca": ca, "cb": cb, "review": rv, "is_new": 1})
        have.add((p["a"], p["b"]))
    for (x, y), rv in (reviews or {}).items():
        if rv == "must_link" and iid in (x, y) and (x, y) not in have:
            p = one_pair(conn, a["run_id"], x, y)
            if p is not None and not conn.execute("SELECT 1 FROM agg_pairs WHERE run_id=? AND a=? AND b=?",
                                                  (run, x, y)).fetchone():
                p["level"] = p["level"] or "review"
                ca, cb = sorted((of(x), of(y)))
                rows.append({**p, "ca": ca, "cb": cb, "review": "must_link", "is_new": 1})
    _insert_pairs(conn, run, ch, rows)
    ch.nodes.add(iid)
    if cur is None:  # fresh material (the common case): increments only, no recomputation
        _campaign_add(conn, run, ch, mc, facts, [p for p in rows if p["ca"] == p["cb"]])
        for p in rows:
            _link_add(conn, run, ch, p)
        return
    touched_c.add(mc)  # a re-decision removed links and members: local recompute of the touched keys
    _refresh_campaigns(conn, run, ch, touched_c)
    keys = {(p["ca"], p["cb"]) for p in rows} | {(p["ca"], p["cb"]) for p in olds}
    _refresh_links(conn, run, ch, {k for k in keys if k[0] != k[1]})


def apply_reviews(conn: sqlite3.Connection, run: str, reviews: dict[tuple[str, str], str], ch: Change) -> None:
    """Bring the review state of the run's pairs to `reviews` (delta: only pairs whose state changed)."""
    current = {(r[0], r[1]): (r[2], r[3], r[4], r[5], r[6], r[7]) for r in conn.execute(
        "SELECT a, b, review, hypothesis, ca, cb, level, is_new FROM agg_pairs WHERE run_id=? AND review IS NOT NULL",
        (run,))}
    touched: set[tuple[str, str]] = set()
    for key in set(current) | set(reviews):
        want = reviews.get(key)
        have = current.get(key)
        if have is not None and have[0] == want:
            continue
        if have is None:
            row = conn.execute("SELECT review, hypothesis, ca, cb, level, is_new FROM agg_pairs WHERE run_id=? AND a=?"
                               " AND b=?", (run, *key)).fetchone()
            if row is None:
                if want != "must_link":
                    continue
                incs = {}
                for x in key:
                    r = conn.execute("SELECT is_new, assign_run FROM agg_incidents WHERE run_id=? AND incident_id=?",
                                     (run, x)).fetchone()
                    if r is not None:
                        incs[x] = {"is_new": bool(r[0]), "assign_run": r[1]}
                p = _accepted_pair(conn, run, key[0], key[1], incs)
                if p is None:
                    continue
                of = _camp_lookup(conn, run)
                ca, cb = sorted((of(key[0]), of(key[1])))
                _insert_pairs(conn, run, ch, [{**p, "ca": ca, "cb": cb}])
                touched.add((ca, cb))
                ch.kinds.add("review")
                continue
            if row[0] == want:
                continue
            have = row
        old_review, hyp, ca, cb, level, is_new = have
        old = {"level": level, "review": old_review, "is_new": is_new}
        _apply_counts(conn, run, ch, pair_counts(old), -1)
        if hyp == "independent" and want != "must_link":  # the link existed only through the must-link
            conn.execute("DELETE FROM agg_pairs WHERE run_id=? AND a=? AND b=?", (run, *key))
            ch.edges.pop(key, None)
        else:
            conn.execute("UPDATE agg_pairs SET review=? WHERE run_id=? AND a=? AND b=?", (want, run, *key))
            _apply_counts(conn, run, ch, pair_counts({**old, "review": want}))
        touched.add((ca, cb))
        ch.kinds.add("review")
    for ca, cb in touched:
        ch.links.add((ca, cb))
    _refresh_links(conn, run, ch, {k for k in touched if k[0] != k[1]})
    _refresh_campaigns(conn, run, ch, {k[0] for k in touched if k[0] == k[1]})


# =============================================================================================== delta messages


def kpis(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Dashboard `kpis` (same shape as /v1/dashboard) from the counters."""
    by: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for r in conn.execute("SELECT dim, key, n, n_new FROM agg_counts WHERE run_id=? AND dim IN ('incidents', 'role',"
                          " 'gene', 'campaigns', 'groups', 'decision', 'enriched')", (run,)):
        by[r[0]][r[1]] = [r[2], r[3]]
    fam: dict[str, dict[str, int]] = defaultdict(dict)
    for key, (n, _) in by["gene"].items():
        f, _, s = key.partition("|")
        fam[f][s] = n
    roles = {k: v[0] for k, v in by["role"].items()}
    return {"incidents": by["incidents"].get("*", [0, 0])[0], "artifacts_by_role": roles,
            "artifacts": sum(roles.values()), "genes_by_family": dict(fam),
            "genes": sum(v[0] for v in by["gene"].values()), "campaigns": by["campaigns"].get("*", [0, 0])[0],
            "multi_member_campaigns": by["campaigns"].get("multi", [0, 0])[0],
            "groups": by["groups"].get("*", [0, 0])[0], "assignments": sum(v[0] for v in by["decision"].values()),
            "enriched_incidents": by["enriched"].get("*", [0, 0])[0],
            "new_incidents": by["incidents"].get("*", [0, 0])[1]}


def counts_of(conn: sqlite3.Connection, run: str, dims: Iterable[str] | None = None) -> dict[str, dict[str, list[int]]]:
    sql, args = "SELECT dim, key, n, n_new FROM agg_counts WHERE run_id=?", [run]
    if dims is not None:
        dims = list(dims)
        sql += f" AND dim IN ({_q(dims)})"
        args += dims
    out: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for r in conn.execute(sql, args):
        out[r[0]][r[1]] = [r[2], r[3]]
    return dict(out)


def top_genes(conn: sqlite3.Connection, run: str, limit: int = TOP_GENES) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute("SELECT gkey, level, df, df_new FROM agg_genes WHERE run_id=? AND df>=2 AND level!='none'"
                          " ORDER BY df DESC, gkey LIMIT ?", (run, limit)):
        fam, kind, _, value = r[0].split(UNIT, 3)
        out.append({"family": fam, "kind": kind, "value": value[:200], "df": r[2], "new": r[3], "level": r[1]})
    return out


def campaign_out(r: Any) -> dict[str, Any]:
    return {"id": r[0], "label": r[1], "group_id": r[2], "solo": bool(r[3]), "size": r[4], "new": r[5],
            "first_seen": r[6], "last_seen": r[7], "internal_edges": r[8], "max_internal_bits": r[9], "family": r[10]}


CAMPAIGN_COLS = ("campaign_id, label, group_id, solo, size, n_new, first_seen, last_seen, internal_edges,"
                 " max_internal_bits, family")


def campaigns_out(conn: sqlite3.Connection, run: str, ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    if ids is None:
        rows = conn.execute(f"SELECT {CAMPAIGN_COLS} FROM agg_campaigns WHERE run_id=?", (run,)).fetchall()
    else:
        ids = sorted(ids)
        rows = []
        for chunk in _chunks(ids):
            rows += conn.execute(f"SELECT {CAMPAIGN_COLS} FROM agg_campaigns WHERE run_id=? AND campaign_id IN"
                                 f" ({_q(chunk)})", [run, *chunk]).fetchall()
    return [campaign_out(r) for r in rows]


def link_out(r: Any) -> dict[str, Any]:
    return {"source": r[0], "target": r[1], "level": r[2], "pairs": r[3], "max_bits": round(r[4], 3),
            "sum_bits": round(r[5], 3), "family_bits": _loads(r[6]), "new": r[7]}


def links_out(conn: sqlite3.Connection, run: str, keys: Iterable[tuple[str, str]] | None = None
              ) -> list[dict[str, Any]]:
    cols = "ca, cb, level, pairs, max_bits, sum_bits, family_bits, n_new"
    if keys is None:
        return [link_out(r) for r in conn.execute(f"SELECT {cols} FROM agg_links WHERE run_id=?", (run,))]
    out = []
    for ca, cb in sorted(keys):
        out += [link_out(r) for r in conn.execute(f"SELECT {cols} FROM agg_links WHERE run_id=? AND ca=? AND cb=?",
                                                  (run, ca, cb))]
    return out


def node_out(iid: str, campaign: str, decision: str | None, assign_run: str | None, data: dict[str, Any],
             camps: dict[str, Any] | None = None) -> dict[str, Any]:
    real = None if campaign.startswith("solo:") else campaign
    return {"id": iid, "title": data.get("title"), "campaign_id": real, "map_campaign": campaign,
            "decision": decision, "observed_at": data.get("day"), "corpus": data.get("corpus"),
            "source": data.get("source"), "thumbnail": data.get("thumb"), "assign_run": assign_run,
            "created_at": (data.get("at") or [None])[0], "new": True}


def nodes_out(conn: sqlite3.Connection, run: str, ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
    sql = "SELECT incident_id, campaign_id, decision, assign_run, data FROM agg_incidents WHERE run_id=? AND is_new=1"
    args: list[Any] = [run]
    if ids is not None:
        ids = sorted(ids)
        if not ids:
            return []
        sql += f" AND incident_id IN ({_q(ids)})"
        args += ids
    rows = conn.execute(sql + " ORDER BY seq DESC, incident_id", args).fetchall()
    return [node_out(r[0], r[1], r[2], r[3], _loads(r[4])) for r in rows]


def edge_out(p: dict[str, Any]) -> dict[str, Any]:
    return {"source": p["a"], "target": p["b"], "bits": p["bits"], "p": p.get("p"), "hypothesis": p.get("hypothesis"),
            "level": p["level"] or None, "family": p.get("family"), "review": p.get("review"), "new": True}


def edges_out(conn: sqlite3.Connection, run: str) -> list[dict[str, Any]]:
    return [edge_out({"a": r[0], "b": r[1], "bits": r[2], "p": r[3], "hypothesis": r[4], "level": r[5],
                      "family": r[6], "review": r[7]}) for r in conn.execute(
        "SELECT a, b, bits, p, hypothesis, level, family, review FROM agg_pairs WHERE run_id=? AND is_new=1"
        " ORDER BY bits DESC", (run,))]


def exports_out(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    by = {k: v[0] for k, v in counts_of(conn, run, ["export"]).get("export", {}).items()}
    return {"total": sum(by.values()), "by_kind": by}


def _message(conn: sqlite3.Connection, run: str, ch: Change, keys: set[tuple[str, str]], version: int,
             seq: int) -> dict[str, Any]:
    counts: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for dim, key in sorted(keys):
        r = conn.execute("SELECT n, n_new FROM agg_counts WHERE run_id=? AND dim=? AND key=?",
                         (run, dim, key)).fetchone()
        counts[dim][key] = [r[0], r[1]] if r is not None else [0, 0]
    msg: dict[str, Any] = {"run_id": run, "v": version, "seq": seq, "from_seq": seq, "n": 1,
                           "kinds": sorted(ch.kinds), "kpis": kpis(conn, run), "counts": dict(counts)}
    if ch.campaigns:
        msg["campaigns"] = campaigns_out(conn, run, ch.campaigns)
        msg["groups"] = [{"id": k, "size": v[0], "new": v[1]} for k, v in counts.get("group", {}).items()]
    if ch.links:
        cross = {k for k in ch.links if k[0] != k[1]}
        rows = links_out(conn, run, cross)
        have = {(x["source"], x["target"]) for x in rows}
        # a key with no rows left is sent as an explicit zero so clients drop the link
        rows += [{"source": ca, "target": cb, "level": None, "pairs": 0, "max_bits": 0.0, "sum_bits": 0.0,
                  "family_bits": {}, "new": 0} for ca, cb in sorted(cross - have)]
        msg["links"] = rows
    if ch.nodes:
        msg["nodes"] = nodes_out(conn, run, ch.nodes)
    if ch.removed_nodes:
        msg["removed_nodes"] = sorted(ch.removed_nodes)
    if ch.edges:
        msg["edges"] = [edge_out(p) for p in sorted(ch.edges.values(), key=lambda p: -p["bits"])]
    if ch.genes:
        msg["top_genes"] = top_genes(conn, run)
    if ch.exports:
        msg["exports"] = exports_out(conn, run)
    if ch.batch:
        msg["batch"] = ch.batch
    return msg


def _emit(conn: sqlite3.Connection, run: str, kind: str, seq: int, version: int, payload: dict[str, Any]) -> None:
    body = _dumps(payload)
    conn.execute("INSERT INTO agg_deltas (run_id, seq, version, kind, bytes, payload, created_at) VALUES"
                 " (?, ?, ?, ?, ?, ?, ?)", (run, seq, version, kind, len(body), body, events.utcnow()))


def _commit_change(conn: sqlite3.Connection, run: str, ch: Change, seq: int) -> None:
    keys = _flush_counts(conn, run, ch)
    if not (keys or ch.campaigns or ch.links or ch.nodes or ch.removed_nodes or ch.genes or ch.exports or ch.edges
            or ch.batch):
        return
    conn.execute("UPDATE agg_state SET version=version+1, seq=?, updated_at=? WHERE run_id=?",
                 (seq, events.utcnow(), run))
    version = conn.execute("SELECT version FROM agg_state WHERE run_id=?", (run,)).fetchone()[0]
    msg = _message(conn, run, ch, keys, version, seq)
    _emit(conn, run, "+".join(sorted(ch.kinds)) or "delta", seq, version, msg)
    n = conn.execute("SELECT COUNT(*) FROM agg_deltas WHERE run_id=?", (run,)).fetchone()[0]
    if n > DELTA_KEEP * 1.1:
        conn.execute("DELETE FROM agg_deltas WHERE id IN (SELECT id FROM agg_deltas WHERE run_id=? ORDER BY id"
                     " LIMIT ?)", (run, n - DELTA_KEEP))


# =============================================================================================== event tap


def on_event(conn: sqlite3.Connection, seq: int, type_: str, payload: dict[str, Any]) -> None:
    """Called by `hydra.events.append` inside the writer transaction of the event. Never raises: a failure rolls
    back only the aggregate part (savepoint) and marks the affected runs stale (rebuilt on next use)."""
    if not enabled():
        return
    try:
        conn.execute("SAVEPOINT dataflow")
    except sqlite3.Error:
        return
    runs: list[str] = []
    try:
        runs = _handle(conn, seq, type_, payload)
        conn.execute("RELEASE dataflow")
    except Exception:  # noqa: BLE001 — the business write must go through; aggregates get rebuilt
        log.warning("dataflow: delta for %s seq %s failed; marking runs stale", type_, seq, exc_info=True)
        try:
            conn.execute("ROLLBACK TO dataflow")
            conn.execute("RELEASE dataflow")
            for run in runs or _affected_runs(conn, type_, payload):
                conn.execute("UPDATE agg_state SET status='stale', updated_at=? WHERE run_id=?",
                             (events.utcnow(), run))
        except sqlite3.Error:
            pass


def _ready_runs(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT run_id FROM agg_state WHERE status='ready' ORDER BY run_id")]


def _affected_runs(conn: sqlite3.Connection, type_: str, payload: dict[str, Any]) -> list[str]:
    try:
        if type_ == "RUN_FINISHED":
            r = conn.execute("SELECT json_extract(params, '$.base_run') FROM runs WHERE id=?",
                             (payload.get("run_id"),)).fetchone()
            base = r[0] if r else None
            return lineage_heirs(conn, base) or ([base] if base else [])
        if type_ == EXPORT_EVENT:
            return [payload["run_id"]] if payload.get("run_id") else []
        return _ready_runs(conn)
    except sqlite3.Error:
        return []


BATCH_KIND = "assign_batch"  # hydra.batch.RUN_KIND
BATCH_ITEMS_MAX = 200


def _batch_finished(conn: sqlite3.Connection, seq: int, run_id: str | None) -> list[str]:
    """merge-batch4: a batch run (variants-pipe `hydra.batch`, `assign_batch`) against a materialised base. Its items
    already arrived as `assign` runs (counted then); the batch adds the cross-comparison result (new pattern ids,
    duplicates) to assignment details, which no aggregate counts. One delta of kind `batch` carries that summary,
    so live clients see the batch result and refresh batch views; the version bump keeps memo keys honest."""
    row = conn.execute("SELECT params, metrics FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return []
    params, metrics = _loads(row[0]), _loads(row[1])
    base = params.get("base_run")
    heirs = lineage_heirs(conn, base)  # CR-E2E-5: the base and every materialised revision of it
    if not heirs:
        return []
    items = [{k: it.get(k) for k in ("incident_id", "decision", "campaign_id", "new_pattern_id", "duplicate_of")}
             for it in metrics.get("items") or [] if isinstance(it, dict)]
    patterns = [{"id": p.get("id"), "size": p.get("size"), "members": list(p.get("members") or [])}
                for p in metrics.get("new_patterns") or [] if isinstance(p, dict)]
    for run in heirs:
        ch = Change(kinds={"batch"}, batch={"run_id": run_id, "base_run": base,
                                            "summary": metrics.get("summary") or {},
                                            "new_patterns": patterns[:BATCH_ITEMS_MAX],
                                            "items": items[:BATCH_ITEMS_MAX],
                                            "truncated": len(items) > BATCH_ITEMS_MAX})
        _commit_change(conn, run, ch, seq)
    return heirs


def _handle(conn: sqlite3.Connection, seq: int, type_: str, payload: dict[str, Any]) -> list[str]:
    if type_ == "RUN_FINISHED":
        run_id = payload.get("run_id")
        if payload.get("kind") == "analyse":
            _emit(conn, "*", "run", seq, 0, {"run_id": run_id, "kind": "analyse", "seq": seq})
            return []
        if payload.get("kind") == BATCH_KIND:
            return _batch_finished(conn, seq, run_id)
        if payload.get("kind") != "assign":
            return []
        r = conn.execute("SELECT json_extract(params, '$.base_run') FROM runs WHERE id=?", (run_id,)).fetchone()
        heirs = lineage_heirs(conn, r[0] if r else None)  # CR-E2E-5: the base and its materialised revisions
        if not heirs:
            return []
        reviews = pair_reviews(conn)
        rows = conn.execute("SELECT incident_id, run_id, decision, campaign_id, created_at FROM assignments WHERE"
                            " run_id=? ORDER BY created_at, incident_id", (run_id,)).fetchall()
        for run in heirs:
            ch = Change()
            for a in rows:
                apply_assignment(conn, run, a[0], {"run_id": a[1], "decision": a[2], "campaign_id": a[3],
                                                   "created_at": a[4]}, seq, reviews, ch)
            _commit_change(conn, run, ch, seq)
        return heirs
    if type_ == EXPORT_EVENT:
        run = payload.get("run_id")
        if not run or not is_ready(conn, run):
            return []
        ch = Change(kinds={"export"}, exports=True)
        kind = payload.get("kind") or "unknown"
        ch.counts[("export", kind)] += 1
        ch.counts[("~new", "export", kind)] += 1
        _commit_change(conn, run, ch, seq)
        return [run]
    # REVIEW / unsure marks: the effective review state may change for pairs of every materialised run
    runs = _ready_runs(conn)
    if not runs:
        return []
    reviews = pair_reviews(conn)
    for run in runs:
        ch = Change()
        apply_reviews(conn, run, reviews, ch)
        _commit_change(conn, run, ch, seq)
    return runs


# =============================================================================================== build


def compute_base(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Off the writer: everything immutable about the run, aggregated."""
    t0 = time.perf_counter()
    st = base_state(conn, run)
    rows = aggregate(st)
    for cid, info in st["campaigns"].items():
        if cid in rows["campaigns"]:
            rows["campaigns"][cid]["meta"] = {"base": info}
    fr = conn.execute("SELECT finished_at FROM runs WHERE id=?", (run,)).fetchone()
    return {"run": run, "rows": rows, "facts": {i: inc["facts"] for i, inc in st["incidents"].items()},
            "finished_at": fr[0] if fr else None, "compute_s": round(time.perf_counter() - t0, 3)}


def install(conn: sqlite3.Connection, base: dict[str, Any]) -> dict[str, Any]:
    """Writer transaction: base rows + catch-up with every delta that exists now (assignments, reviews, exports)."""
    t0 = time.perf_counter()
    run = base["run"]
    prev = state_row(conn, run)
    _clear(conn, run)
    _write(conn, run, base["rows"], base["facts"], {})
    seq = _max_seq(conn)
    version = (prev["version"] if prev else 0) + 1
    now = events.utcnow()
    conn.execute("INSERT OR REPLACE INTO agg_state (run_id, status, version, seq, base_seq, base_finished_at,"
                 " built_at, updated_at, meta) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?)",
                 (run, version, seq, seq, base["finished_at"], now, now,
                  _dumps({"compute_s": base["compute_s"]})))
    ch = Change()
    latest = latest_assignments(conn, run)
    reviews = pair_reviews(conn)
    for iid, a in sorted(latest.items(), key=lambda kv: (kv[1]["created_at"], kv[1]["run_id"], kv[0])):
        if a["run_id"] != run:
            apply_assignment(conn, run, iid, a, seq, reviews, ch)
    apply_reviews(conn, run, reviews, ch)
    for kind, n in export_counts(conn, run).items():
        ch.counts[("export", kind)] += n
        ch.counts[("~new", "export", kind)] += n
    _flush_counts(conn, run, ch)
    _emit(conn, run, "reset", seq, version, {"run_id": run, "reason": "rebuilt", "v": version, "seq": seq})
    install_s = round(time.perf_counter() - t0, 3)
    conn.execute("UPDATE agg_state SET meta=? WHERE run_id=?",
                 (_dumps({"compute_s": base["compute_s"], "install_s": install_s}), run))
    return {"run_id": run, "version": version, "seq": seq, "compute_s": base["compute_s"], "install_s": install_s,
            "incidents": len(base["facts"]), "pairs": len(base["rows"]["pairs"]),
            "gene_values": len(base["rows"]["genes"]), "campaigns": len(base["rows"]["campaigns"]),
            "links": len(base["rows"]["links"])}


def build(db_path: Path, writer: Any, run: str) -> dict[str, Any]:
    """Compute on a read-only connection (this thread), install through the single Writer."""
    from hydra.db import connect

    with closing(connect(db_path, readonly=True)) as conn:
        base = compute_base(conn, run)
    return writer.run(lambda c: install(c, base), timeout=600)


def build_safely(db_path: Path, writer: Any, run: str | None) -> dict[str, Any] | None:
    """`build` for the end of analyse / review: never fails the caller."""
    if not run or not enabled():
        return None
    try:
        out = build(db_path, writer, run)
        log.info("dataflow: built %s", out)
        return out
    except Exception:  # noqa: BLE001
        log.warning("dataflow: building aggregates of %s failed", run, exc_info=True)
        return None


_BUILDING: set[str] = set()
_BUILD_LOCK = threading.Lock()
_RUN_LOCKS: dict[str, threading.Lock] = {}


def ensure(db_path: Path, writer: Any, conn: sqlite3.Connection, run: str | None) -> bool:
    """True when the run has ready aggregates; materialises them first when missing or stale (once per run, other
    callers wait on the same lock). Only finished analyse runs are materialised."""
    if not run or not enabled():
        return False
    if is_ready(conn, run):
        return True
    try:
        r = conn.execute("SELECT kind, finished_at FROM runs WHERE id=?", (run,)).fetchone()
        conn.execute("SELECT 1 FROM agg_state LIMIT 1")
    except sqlite3.OperationalError:  # schema older than v5
        return False
    if r is None or r[0] != "analyse" or not r[1] or writer is None:
        return False
    with _BUILD_LOCK:
        lock = _RUN_LOCKS.setdefault(run, threading.Lock())
        _BUILDING.add(run)
    try:
        with lock:
            if not is_ready(conn, run):
                build_safely(db_path, writer, run)
    finally:
        with _BUILD_LOCK:
            _BUILDING.discard(run)
    return is_ready(conn, run)


def ensure_async(db_path: Path, writer: Any, run: str | None) -> bool:
    """Schedule a background build when the run has no ready aggregates (old runs, stale after an error)."""
    if not run or not enabled():
        return False
    with _BUILD_LOCK:
        if run in _BUILDING:
            return True
        _BUILDING.add(run)

    def go() -> None:
        try:
            build_safely(db_path, writer, run)
        finally:
            with _BUILD_LOCK:
                _BUILDING.discard(run)

    threading.Thread(target=go, name=f"hydra-dataflow-{run[-6:]}", daemon=True).start()
    return True


def building(run: str | None) -> bool:
    with _BUILD_LOCK:
        return bool(run) and run in _BUILDING


def live_info(conn: sqlite3.Connection, run: str | None) -> dict[str, Any]:
    """The `live` block of GET responses (docs/ui/STREAM_API.md)."""
    st = state_row(conn, run) if run and is_ready(conn, run) else None
    if st is None:
        return {"run_id": run, "version": 0, "seq": 0, "status": "building" if building(run) else "missing",
                "base_finished_at": None, "new_incidents": 0, "by_decision": {}, "label_pl": "nowe od analizy"}
    c = counts_of(conn, run, ["incidents", "decision"])
    return {"run_id": run, "version": st["version"], "seq": st["seq"], "status": st["status"],
            "base_finished_at": st["base_finished_at"], "new_incidents": c.get("incidents", {}).get("*", [0, 0])[1],
            "by_decision": {k: v[1] for k, v in c.get("decision", {}).items() if v[1]},
            "label_pl": "nowe od analizy"}


def verify(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Compare the materialised aggregates with a full recomputation. Returns differing keys per table."""
    a, b = normalise(snapshot(conn, run)), normalise(recompute(conn, run))
    diff = {}
    for name in a:
        ka, kb = a[name], b[name]
        bad = [k for k in set(ka) | set(kb) if ka.get(k) != kb.get(k)]
        if bad:
            diff[name] = [(str(k), ka.get(k), kb.get(k)) for k in sorted(bad, key=str)[:5]] + [len(bad)]
    return {"run_id": run, "ok": not diff, "diff": diff}


# =============================================================================================== read views
# Overlays of the delta layer on the cached base views (API-PERF cache, base-only fingerprint). Same rules as the
# UI patches (ui/src/api/stream.ts), so a refetch and a live patch give the same numbers.

FNV_OFFSET, FNV_PRIME = 2166136261, 16777619


def fnv1a32(text: str) -> int:
    h = FNV_OFFSET
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * FNV_PRIME) & 0xFFFFFFFF
    return h


def place(iid: str, disc: Any, bounds: dict[str, float] | None) -> tuple[float, float]:
    """Position of a new node (docs/ui/STREAM_API.md): on 0.8 r of its campaign disc, else on a ring outside."""
    import math

    t = 2 * math.pi * (fnv1a32(iid) % 3600) / 3600
    if disc is not None and disc[0] is not None:
        cx, cy, r = float(disc[0]), float(disc[1]), float(disc[2] or 0.8)
        return round(cx + 0.8 * r * math.cos(t), 3), round(cy + 0.8 * r * math.sin(t), 3)
    b = bounds or {}
    rr = (max(abs(float(b.get(k) or 0)) for k in ("x0", "x1", "y0", "y1")) if b else 0.0) + 4
    return round(rr * math.cos(t), 3), round(rr * math.sin(t), 3)


def _new_rows(conn: sqlite3.Connection, run: str) -> list[tuple[str, str, str | None, str | None, dict[str, Any]]]:
    return [(r[0], r[1], r[2], r[3], _loads(r[4])) for r in conn.execute(
        "SELECT incident_id, campaign_id, decision, assign_run, data FROM agg_incidents WHERE run_id=? AND is_new=1"
        " ORDER BY seq, incident_id", (run,))]


def dashboard_view(conn: sqlite3.Connection, run: str, heavy: dict[str, Any]) -> dict[str, Any]:
    """/v1/dashboard: cached base part + current totals + new material marked `new`."""
    from hydra.api_task import FAMILY_ORDER

    out = dict(heavy)
    k = kpis(conn, run)
    k.pop("new_incidents", None)
    out["kpis"] = {**heavy["kpis"], **k}
    rows = _new_rows(conn, run)
    agg = {c["id"]: c for c in campaigns_out(conn, run) if not c["solo"]}
    order = {c["id"]: i for i, c in enumerate(heavy.get("campaigns") or [])}
    by_c: dict[str, list[dict[str, Any]]] = defaultdict(list)
    previews = list(heavy.get("previews") or [])
    coverage = list(heavy.get("coverage") or [])
    for iid, camp, decision, _arun, d in rows:
        real = None if camp.startswith("solo:") else camp
        previews.append({"id": iid, "title": d.get("title"), "observed_at": d.get("observed_at"),
                         "thumbnail": d.get("thumb"), "source": d.get("source"), "scheme": None, "campaign_id": real,
                         "campaign_index": order.get(real or ""), "video": None, "new": True, "decision": decision})
        fam: dict[str, dict[str, int]] = defaultdict(dict)
        for key, n in (d.get("genes") or {}).items():
            f, _, s = key.partition("|")
            fam[f][s] = n
        coverage.append({"incident_id": iid, "families": {f: fam.get(f, {}) for f in FAMILY_ORDER}, "new": True})
        if real:
            by_c[real].append({"id": iid, "thumbnail": d.get("thumb"), "new": True})
    camps = []
    for c in heavy.get("campaigns") or []:
        a = agg.get(c["id"])
        extra = by_c.get(c["id"], [])
        camps.append({**c, "size": a["size"] if a else c["size"], "new": a["new"] if a else 0,
                      "members": [*c.get("members", []), *extra]})
    out.update(previews=previews, coverage=coverage, campaigns=camps, live=live_info(conn, run),
               top_genes=top_genes(conn, run))
    return out


def flow_counts(conn: sqlite3.Connection, run: str) -> dict[str, Any]:
    """Current values of the counters the "Jak to działa" boxes show (base + new material)."""
    c = counts_of(conn, run, ["incidents", "corpus", "role", "family", "enr_genes", "tier"])

    def one(dim: str) -> dict[str, int]:
        return {k: v[0] for k, v in c.get(dim, {}).items()}

    return {"n_scope": c.get("incidents", {}).get("*", [0, 0])[0], "sources": one("corpus"), "roles": one("role"),
            "fam": one("family"), "enr_genes": c.get("enr_genes", {}).get("*", [0, 0])[0], "levels": one("tier"),
            "new": {"incidents": c.get("incidents", {}).get("*", [0, 0])[1],
                    "by_corpus": {k: v[1] for k, v in c.get("corpus", {}).items() if v[1]}}}


def assigned_members(conn: sqlite3.Connection, run: str, campaign_ids: list[str] | None = None) -> dict[str, list[str]]:
    """Incidents attached to a campaign of `run` after the analysis (assign / assign-batch decisions `assign`), per
    campaign, in arrival order. They count in the live `size` but are not `campaign_members` of the run (audit
    B15). {} when the aggregate has not been built for the run."""
    sql = ("SELECT campaign_id, incident_id FROM agg_incidents WHERE run_id=? AND is_new=1 AND decision='assign'")
    args: list[Any] = [run]
    if campaign_ids is not None:
        if not campaign_ids:
            return {}
        sql += f" AND campaign_id IN ({','.join('?' * len(campaign_ids))})"
        args += campaign_ids
    out: dict[str, list[str]] = defaultdict(list)
    try:
        for cid, iid in conn.execute(sql + " ORDER BY seq, incident_id", args):
            out[cid].append(iid)
    except sqlite3.OperationalError:  # older state without the aggregate tables
        return {}
    return dict(out)


def campaigns_page_view(conn: sqlite3.Connection, run: str, items: list[dict[str, Any]]) -> None:
    """/v1/campaigns items in place: current size and the new members count. `members` lists the clustered
    members and then the material assigned later (also in `assigned_members`), so `size == len(members)` whenever
    the stored member list is complete (audit B15); `base_size` is the size at the analysis."""
    ids = [i["id"] for i in items]
    agg = {c["id"]: c for c in campaigns_out(conn, run, ids)}
    assigned = assigned_members(conn, run, ids)
    for it in items:
        a = agg.get(it["id"])
        base = it.get("size")
        if a is not None:
            it["size"], it["new"] = a["size"], a["new"]
        else:
            it["new"] = 0
        extra = assigned.get(it["id"], [])
        members = it.get("members")
        if isinstance(members, list) and all(isinstance(m, str) for m in members):
            known = set(members)
            it["members"] = [*members, *(x for x in extra if x not in known)]
        it["assigned_members"] = extra
        it["base_size"] = base


def _link_merge(rows: list[dict[str, Any]], allowed: set[str]) -> dict[str, Any] | None:
    rows = [r for r in rows if r["level"] in allowed and r["pairs"] > 0]
    if not rows:
        return None
    fam: dict[str, float] = defaultdict(float)
    for r in rows:
        for f, v in (r["family_bits"] or {}).items():
            fam[f] += v
    best = max(rows, key=lambda r: LEVEL_RANK.get(r["level"], 0))["level"]
    return {"pairs": sum(r["pairs"] for r in rows), "max_bits": round(max(r["max_bits"] for r in rows), 3),
            "sum_bits": round(sum(r["sum_bits"] for r in rows), 3), "family": _best(fam), "level": best,
            "new": sum(r["new"] for r in rows)}


def overview_view(conn: sqlite3.Connection, run: str, payload: dict[str, Any], levels: str, min_bits: float,
                  min_pairs: int) -> dict[str, Any]:
    """/v1/graph/overview: base payload (API-PERF) + campaign sizes, links and nodes of new material."""
    out = dict(payload)
    cols = list(payload["campaigns"]["columns"])
    ci = {c: i for i, c in enumerate(cols)}
    agg = {c["id"]: c for c in campaigns_out(conn, run)}
    rows = []
    for r in payload["campaigns"]["rows"]:
        r = list(r)
        a = agg.get(r[ci["id"]])
        if a is not None:
            r[ci["size"]] = a["size"]
            if a["new"]:
                for col in ("first_seen", "last_seen", "internal_edges", "max_internal_bits", "family"):
                    r[ci[col]] = a[col]
        rows.append([*r, a["new"] if a else 0])
    discs = {r[ci["id"]]: (r[ci["x"]], r[ci["y"]], r[ci["r"]]) for r in rows}
    bounds = payload.get("bounds")
    have = set(discs)
    new_rows = _new_rows(conn, run)
    thumbs = {iid: d.get("thumb") for iid, _c, _dec, _arun, d in new_rows}
    for c in sorted(agg.values(), key=lambda c: c["id"]):
        if c["solo"] and c["new"] and c["id"] not in have:
            x, y = place(c["id"][5:], None, bounds)
            row = {"id": c["id"], "label": c["label"], "confidence": None, "group_id": None, "size": c["size"],
                   "first_seen": c["first_seen"], "last_seen": c["last_seen"], "scheme": None, "x": x, "y": y,
                   "r": 0.8, "internal_edges": c["internal_edges"], "max_internal_bits": c["max_internal_bits"],
                   "family": c["family"], "thumbnail": thumbs.get(c["id"][5:]), "solo": True}
            rows.append([row.get(col) for col in cols] + [c["new"]])
            discs[c["id"]] = (x, y, 0.8)
    out["campaigns"] = {"columns": [*cols, "new"], "rows": rows}
    lcols = list(payload["links"]["columns"])
    li = {c: i for i, c in enumerate(lcols)}
    links = {(r[li["source"]], r[li["target"]]): [*r, 0] for r in payload["links"]["rows"]}
    if min_bits <= 0:
        allowed = {x for x in levels.split(",") if x}
        by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in conn.execute("SELECT ca, cb, level, pairs, max_bits, sum_bits, family_bits, n_new FROM agg_links"
                              " WHERE run_id=? AND (ca, cb) IN (SELECT ca, cb FROM agg_links WHERE run_id=? AND"
                              " n_new>0)", (run, run)):
            by[(r[0], r[1])].append(link_out(r))
        for key, lr in by.items():
            m = _link_merge(lr, allowed)
            if m is None or m["pairs"] < min_pairs:
                links.pop(key, None)
                continue
            links[key] = [key[0], key[1], m["pairs"], m["max_bits"], m["sum_bits"], m["family"], m["level"], m["new"]]
    out["links"] = {"columns": [*lcols, "new"],
                    "rows": sorted(links.values(), key=lambda r: (-r[li["max_bits"]], r[0], r[1]))}
    nodes = []
    for iid, camp, decision, arun, d in new_rows:
        n = node_out(iid, camp, decision, arun, d)
        if camp.startswith("solo:"):
            disc = discs.get(camp)
            n["x"], n["y"] = (disc[0], disc[1]) if disc else place(iid, None, bounds)
        else:
            n["x"], n["y"] = place(iid, discs.get(camp), bounds)
        nodes.append(n)
    stats = dict(payload.get("stats") or {})
    if stats:
        lv = counts_of(conn, run, ["level", "review"])
        stats["nodes"] = int(stats.get("nodes") or 0) + len(nodes)
        stats["edges"] = int(stats.get("edges") or 0) + sum(v[1] for k, v in lv.get("level", {}).items() if k != "-")
        levels_ = dict(stats.get("levels") or {})
        for k, v in lv.get("level", {}).items():
            if v[1] and k != "-":
                levels_[k] = levels_.get(k, 0) + v[1]
        stats["levels"] = levels_
        for name in ("must_link", "cannot_link"):
            stats[name] = int(stats.get(name) or 0) + lv.get("review", {}).get(name, [0, 0])[1]
    xs = [(d[0], d[1], d[2] or 0.8) for cid, d in discs.items() if cid not in have and d[0] is not None]
    if bounds and xs:  # `bounds` covers the new discs; placement keeps using the base box (`base_bounds`)
        out["bounds"] = {"x0": min(bounds["x0"], *(x - r for x, _, r in xs)),
                         "y0": min(bounds["y0"], *(y - r for _, y, r in xs)),
                         "x1": max(bounds["x1"], *(x + r for x, _, r in xs)),
                         "y1": max(bounds["y1"], *(y + r for _, y, r in xs))}
    out.update(stats=stats or payload.get("stats"), levels=levels, min_bits=min_bits, base_bounds=bounds,
               live={**live_info(conn, run), "nodes": nodes, "edges": edges_out(conn, run)})
    return out


def live_graph(conn: sqlite3.Connection, run: str, base: Any) -> Any:
    """The incident-level graph base (hydra.graph_lod.GraphBase, base scope) with the new material merged in:
    node rows built by `graph_lod.node_rows`, positions by `place` (solo discs on a ring outside the base bounds),
    edge rows with reasons read from the assign runs' pairs. Explore / campaign / neighbors / viewport then treat
    new incidents like any other; the overview uses `overview_view` instead."""
    from hydra import graph_lod as lod

    rows = [r for r in _new_rows(conn, run) if r[0] not in base.node_by_id]
    if not rows:
        return base
    members = {iid: camp for iid, camp, *_ in rows if not camp.startswith("solo:") and camp in base.campaigns}
    nrows = lod.node_rows(conn, [r[0] for r in rows], members, base.campaigns)
    bounds = lod.bounds_of(base.layout)
    layout = {"nodes": dict(base.layout["nodes"]), "campaigns": dict(base.layout["campaigns"]),
              "groups": base.layout["groups"]}
    camps = {k: dict(v) for k, v in base.campaigns.items()}
    for iid, *_ in rows:
        cid = members.get(iid)
        if cid is not None:
            x, y = place(iid, layout["campaigns"].get(cid), bounds)
            camps[cid]["size"] = camps[cid].get("size", 0) + 1
        else:
            x, y = place(iid, None, bounds)
            layout["campaigns"][solo(iid)] = [x, y, 0.8]
        layout["nodes"][iid] = [x, y]
    raw = {**base.raw, "nodes": [*base.nodes, *nrows], "edges": [*base.edges, *_live_edges(conn, run)],
           "layout": layout, "campaigns": camps, "live_merged": True}
    return lod.GraphBase(raw)


def _live_edges(conn: sqlite3.Connection, run: str) -> list[list[Any]]:
    from hydra import graph_lod as lod

    edges = []
    for r in conn.execute("SELECT a, b, src_run, level FROM agg_pairs WHERE run_id=? AND is_new=1 ORDER BY bits DESC",
                          (run,)).fetchall():
        p = conn.execute("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=? AND a=?"
                         " AND b=?", (r[2], r[0], r[1])).fetchone()
        if p is None:
            continue
        e = lod.edge_from_row(p[0], p[1], p[2], p[3], p[4], lod.loads(p[5]))
        e[lod.E_LEVEL] = e[lod.E_LEVEL] or (r[3] or None)
        edges.append(e)
    return edges


def _main(argv: list[str] | None = None) -> int:  # pragma: no cover — operator tool
    import argparse
    import json

    from hydra.config import get_settings
    from hydra.db import Writer, connect

    ap = argparse.ArgumentParser(prog="python -m hydra.dataflow", description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["rebuild", "verify", "status"])
    ap.add_argument("run", nargs="*", help="run ids (default: latest analyse run of every corpus)")
    args = ap.parse_args(argv)
    s = get_settings()
    with closing(connect(s.db_path, readonly=True)) as conn:
        runs = args.run or [r[0] for r in conn.execute(
            "SELECT id FROM runs WHERE kind='analyse' AND finished_at IS NOT NULL ORDER BY created_at DESC")]
    if args.cmd == "rebuild":
        from hydra.scheduler import api_running

        if api_running(s):
            print("an API process is running on this state: it owns the single Writer; stop it first")
            return 2
        w = Writer(s.db_path)
        try:
            for run in runs:
                print(json.dumps(build(s.db_path, w, run)))
        finally:
            w.close()
        return 0
    with closing(connect(s.db_path, readonly=True)) as conn:
        for run in runs:
            if args.cmd == "status":
                print(json.dumps(state_row(conn, run)))
            else:
                out = verify(conn, run) if is_ready(conn, run) else {"run_id": run, "ok": None, "status": "missing"}
                print(json.dumps(out, default=str)[:2000])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
