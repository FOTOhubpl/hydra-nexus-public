"""Relationship-graph explorer API (GRAPH-UI stream). Read-only views over the latest analyse run.

- GET  /v1/graph/explore      compact incident graph: nodes, evidence-level edges (campaign / kit / asset / weak),
                              robustness, analyst must-link / cannot-link, campaigns, groups, stats
- GET  /v1/graph/path         strongest evidence path between two incidents (campaign + kit levels) with the genes
                              connecting each hop
- GET  /v1/graph/indicators   bipartite incidents <-> shared indicators, over-correlating values flagged
- POST /v1/graph/export       subgraph export (JSON or Neo4j Cypher) for a set of incident ids
- GET  /v1/graph/campaigns/{id}/summary   rule-based "why this cluster" summary (no LLM)

Every number is read from `pairs.detail` / `genes` / `campaigns`; nothing is estimated here.
"""

from __future__ import annotations

import heapq
import json
import re
import sqlite3
from collections import Counter, defaultdict
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from hydra import api_cache
from hydra import graph_lod as lod
from hydra.api_task import (
    _campaign_card,
    _dominant_family,
    _groups,
    _in,
    _json,
    _membership,
    _pair_reviews,
    _st,
    corpus_params,
    resolve_run,
    run_corpus,
    run_incidents,
)
from hydra.export_log import record_export
from hydra.redact import redact_obj

router = APIRouter(prefix="/v1/graph", tags=["graph"])

LINK_BITS = 10.0  # synapse.Thresholds.link (campaign-level bits)
KIT_BITS = 6.0    # synapse.Thresholds.kit
LEVEL_OF = {"same_campaign": "campaign", "shared_template": "kit", "shared_asset": "asset", "abstain": "weak"}
LEVEL_PL = {"campaign": "kampania", "kit": "zestaw / szablon", "asset": "wspólny zasób", "weak": "słabe / do oceny",
            "review": "decyzja analityka"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
THUMB_ROLES = ("ad_creative", "feed_screenshot", "landing_screenshot")
INDICATOR_KINDS = ("tracker_id", "landing_host", "registrable_domain", "linked_host", "asset_sha256", "entity_phone",
                   "url_path_skeleton", "url_label_skeleton", "favicon_mmh3", "ns_set", "registrar", "operator_id",
                   "person_named", "persona_renamed", "face_cluster")
INDICATOR_KIND_PL = {
    "tracker_id": "piksel / ID śledzący", "landing_host": "host strony docelowej",
    "registrable_domain": "domena", "linked_host": "host linkowany", "asset_sha256": "hash zasobu",
    "entity_phone": "telefon", "url_path_skeleton": "szkielet URL", "url_label_skeleton": "szkielet hosta",
    "favicon_mmh3": "favicon", "ns_set": "serwery NS", "registrar": "rejestrator", "operator_id": "ID operatora",
    "person_named": "osoba (nazwisko)", "persona_renamed": "persona", "face_cluster": "twarz",
}


def _corpus_of(source: str | None) -> str:
    s = (source or "").lower()
    if s.startswith("nask"):
        return "nask"
    if "public" in s or "cert" in s:
        return "public"
    if s == "synthetic":
        return "synthetic"
    return "ext"


def _level(hypothesis: str, detail: dict[str, Any]) -> str | None:
    lvl = LEVEL_OF.get(hypothesis)
    kit = float(detail.get("kit_bits") or 0) >= KIT_BITS
    if lvl in ("weak", "asset") and kit:
        return "kit"
    return lvl


def _inferred(reasons: list[dict[str, Any]]) -> bool:
    """Enrichment-inferred link: most positive reason bits come from enrichment records (WHOIS/DNS/TLS/...)."""
    pos = [r for r in reasons if (r.get("bits") or 0) > 0]
    total = sum(r["bits"] for r in pos)
    enr = sum(r["bits"] for r in pos if ((r.get("evidence_a") or {}).get("enrichment_key")
                                         or (r.get("evidence_b") or {}).get("enrichment_key")))
    return total > 0 and enr / total > 0.5


def _short_reason(r: dict[str, Any]) -> dict[str, Any]:
    return {"family": r.get("family"), "kind": r.get("kind"), "bits": round(float(r.get("bits") or 0), 2),
            "value": (r.get("value_a") or r.get("value_b") or "")[:120] or None, "doc_freq": r.get("doc_freq"),
            "text": r.get("explanation_pl") or r.get("kind")}


def _robust(detail: dict[str, Any]) -> dict[str, Any] | None:
    rob = detail.get("robustness") or (detail.get("meta") or {}).get("robustness")
    if not rob:
        return None
    return {"robust": bool(rob.get("robust_link")), "critical": list(rob.get("critical_families") or []),
            "link": rob.get("link")}


def _edge_row(r: sqlite3.Row, reviews: dict[tuple[str, str], str], top: int) -> dict[str, Any] | None:
    detail = _json(r["detail"], {}) or {}
    review = reviews.get((r["a"], r["b"]))
    level = _level(r["hypothesis"], detail)
    if level is None and review != "accepted":
        return None
    fam_bits = detail.get("family_bits") or {}
    reasons = [x for x in detail.get("reasons") or [] if isinstance(x, dict)]
    return {
        "id": f"{r['a']}|{r['b']}", "source": r["a"], "target": r["b"],
        "bits": round(float(r["total_bits"]), 3), "p": r["probability"], "hypothesis": r["hypothesis"],
        "level": level or "review", "family": _dominant_family(fam_bits),
        "family_bits": {k: round(float(v), 2) for k, v in fam_bits.items() if v},
        "campaign_bits": detail.get("campaign_bits"), "kit_bits": detail.get("kit_bits"),
        "asset_bits": detail.get("asset_bits"),
        "inferred": _inferred(reasons), "robustness": _robust(detail),
        "review": {"accepted": "must_link", "rejected": "cannot_link"}.get(review or ""),
        "reasons": [_short_reason(x) for x in sorted(reasons, key=lambda x: -(x.get("bits") or 0))[:top]],
    }


def _thumbs(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    """One preview per incident by role order (ad > feed > landing screenshot), else the largest image."""
    out: dict[str, tuple[int, int, str]] = {}
    order = {r: i for i, r in enumerate(THUMB_ROLES)}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in conn.execute(
            "SELECT ia.incident_id AS iid, ia.artifact_id AS aid, ia.role AS role, a.size AS size"
            " FROM incident_artifacts ia JOIN artifacts a ON a.id=ia.artifact_id"
            f" WHERE ia.incident_id IN ({_in(chunk)}) AND a.mime LIKE 'image/%' AND a.mime!='image/svg+xml'", chunk
        ):
            key = (order.get(r["role"], 9), -(r["size"] or 0), r["aid"])
            if r["iid"] not in out or key < out[r["iid"]]:
                out[r["iid"]] = key
    return {k: v[2] for k, v in out.items()}


def _load(conn: sqlite3.Connection, run: str, *, top: int = 3) -> dict[str, Any]:
    members = _membership(conn, run)
    reviews = _pair_reviews(conn)
    camps = {}
    for r in conn.execute("SELECT id, label, confidence, detail FROM campaigns WHERE run_id=?", (run,)):
        d = _json(r["detail"], {}) or {}
        camps[r["id"]] = {"id": r["id"], "label": r["label"], "confidence": r["confidence"],
                          "group_id": d.get("group_id"), "first_seen": d.get("first_seen"),
                          "last_seen": d.get("last_seen"), "scheme": d.get("scheme"), "size": 0}
    groups = _groups(conn, run)
    group_of = {cid: g["id"] for g in groups for cid in (g.get("campaign_ids") or [])}
    for c in camps.values():
        c["group_id"] = c["group_id"] or group_of.get(c["id"])
    edges = [e for r in conn.execute(
        "SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs"
        " WHERE run_id=? AND hypothesis!='independent' ORDER BY total_bits DESC", (run,))
        if (e := _edge_row(r, reviews, top)) is not None]
    # analyst-accepted pairs scored independent still count (must-link)
    accepted = [k for k, v in reviews.items() if v == "accepted"]
    have = {(e["source"], e["target"]) for e in edges}
    for a, b in accepted:
        if (a, b) in have:
            continue
        r = conn.execute("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=? AND a=?"
                         " AND b=?", (run, a, b)).fetchone()
        if r is not None and (e := _edge_row(r, reviews, top)) is not None:
            edges.append(e)
    ids = sorted(set(run_incidents(conn, run)) | {x for e in edges for x in (e["source"], e["target"])})
    reg: dict[str, str] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in conn.execute(
            "SELECT incident_id, MIN(value) AS v FROM genes WHERE kind='registration_day' AND status='observed'"
            f" AND incident_id IN ({_in(chunk)}) GROUP BY incident_id", chunk):
            if r["v"] and DATE_RE.match(r["v"]):
                reg[r["incident_id"]] = r["v"][:10]
    inc: dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        inc.update({r["id"]: r for r in conn.execute(
            f"SELECT id, title, observed_at, source FROM incidents WHERE id IN ({_in(chunk)})", chunk)})
    thumbs = _thumbs(conn, ids)
    degree = Counter([e["source"] for e in edges] + [e["target"] for e in edges])
    nodes = []
    for n in ids:
        row = inc.get(n)
        cid = members.get(n)
        if cid in camps:
            camps[cid]["size"] += 1
        obs = row["observed_at"] if row is not None and row["observed_at"] else None
        nodes.append({
            "id": n, "title": row["title"] if row is not None else None, "campaign_id": cid,
            "group_id": camps[cid]["group_id"] if cid in camps else None,
            "observed_at": obs[:10] if obs and DATE_RE.match(obs) else None, "registered_at": reg.get(n),
            "corpus": _corpus_of(row["source"] if row is not None else None), "thumbnail": thumbs.get(n),
            "degree": degree[n],
        })
    return {"members": members, "campaigns": camps, "groups": groups, "edges": edges, "nodes": nodes}


def _components(node_ids: list[str], edges: list[dict[str, Any]]) -> int:
    parent = {n: n for n in node_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        if e["source"] in parent and e["target"] in parent:
            parent[find(e["source"])] = find(e["target"])
    return len({find(n) for n in node_ids})


def _stats(node_ids: list[str], edges: list[dict[str, Any]], camps: list[dict[str, Any]], n_groups: int
           ) -> dict[str, Any]:
    levels = Counter(e["level"] for e in edges)
    rob = [e["robustness"] for e in edges if e["robustness"] is not None]
    largest = camps[0] if camps else None
    return {
        "nodes": len(node_ids), "edges": len(edges), "components": _components(node_ids, edges),
        "campaigns": len(camps), "multi_campaigns": sum(1 for c in camps if c["size"] > 1),
        "groups": n_groups, "levels": dict(levels),
        "largest_campaign": {"id": largest["id"], "label": largest["label"], "size": largest["size"]}
        if largest else None,
        "robustness_scored": len(rob), "robust": sum(1 for x in rob if x["robust"]),
        "must_link": sum(1 for e in edges if e["review"] == "must_link"),
        "cannot_link": sum(1 for e in edges if e["review"] == "cannot_link"),
        "inferred": sum(1 for e in edges if e["inferred"]),
    }


def _stats_rows(node_ids: list[str], rows: list[list[Any]], camps: list[dict[str, Any]], n_groups: int
                ) -> dict[str, Any]:
    """`_stats` on compact overlay rows (EDGE_COLUMNS order), without building edge dicts."""
    largest = camps[0] if camps else None
    rob = [e[12] for e in rows if e[12] is not None]
    return {
        "nodes": len(node_ids), "edges": len(rows),
        "components": _components(node_ids, [{"source": e[0], "target": e[1]} for e in rows]),
        "campaigns": len(camps), "multi_campaigns": sum(1 for c in camps if c["size"] > 1),
        "groups": n_groups, "levels": dict(Counter(e[5] for e in rows)),
        "largest_campaign": {"id": largest["id"], "label": largest["label"], "size": largest["size"]}
        if largest else None,
        "robustness_scored": len(rob), "robust": sum(1 for x in rob if x[0]),
        "must_link": sum(1 for e in rows if e[13] == "must_link"),
        "cannot_link": sum(1 for e in rows if e[13] == "cannot_link"),
        "inferred": sum(1 for e in rows if e[11]),
    }


_LIVE_BASES: dict[str, tuple[Any, lod.GraphBase]] = {}


def _graph_base(st: Any, conn: sqlite3.Connection, run: str, info: dict[str, Any] | None = None,
                live: bool = True) -> tuple[lod.GraphBase, bool]:
    """Per-run compact graph (cached on disk under work/cache/api and in memory); no review state inside.
    DATAFLOW: with materialised aggregates the base covers the immutable run only (an assignment never invalidates
    it); `GraphBase.overlay` adds new material and memo keys carry the aggregate version."""
    from hydra import dataflow

    if info is None:
        info = {}
    if dataflow.ensure(st.settings.db_path, getattr(st, "writer", None), conn, run):
        st_ = dataflow.state_row(conn, run) or {}
        info["live_version"] = st_.get("version")
        base, hit = api_cache.cached(st.settings, conn, "graph-base", run, {"top": lod.BASE_TOP, "scope": "base"},
                                     lambda: {**lod.build_base(conn, run, live=False), "scope": "base"},
                                     wrap=lod.GraphBase.from_json, info=info, base_only=True)
        if not live:
            return base, hit
        key = (id(base), st_.get("version"))
        got = _LIVE_BASES.get(run)
        if got is None or got[0] != key:
            got = _LIVE_BASES[run] = (key, dataflow.live_graph(conn, run, base))
            while len(_LIVE_BASES) > 8:
                _LIVE_BASES.pop(next(iter(_LIVE_BASES)))
        return got[1], hit
    return api_cache.cached(st.settings, conn, "graph-base", run, {"top": lod.BASE_TOP},
                            lambda: lod.build_base(conn, run), wrap=lod.GraphBase.from_json, info=info)


def _memo_key(endpoint: str, run: str, info: dict[str, Any], reviews: dict[tuple[str, str], str],
              **params: Any) -> list[Any] | None:
    """Response memo key; None when the run is not cacheable (unfinished / cache disabled)."""
    if info.get("fp") is None:
        return None
    return [endpoint, run, info["fp"], api_cache.reviews_digest(reviews), info.get("live_version"), params]


def _respond(request: Request, key: list[Any] | None, build: Any, hit: bool) -> Any:
    if key is None:
        return api_cache.json_response(build(), hit=False)
    return api_cache.memo_response(request, key, build, hit=hit)


_GZIP_CLIENT = SimpleNamespace(headers={"accept-encoding": "gzip"})


def warm_run(st: Any, conn: sqlite3.Connection, run: str) -> None:
    """Graph base on disk + the default overview / explore responses in the memo (same keys as the endpoints)."""
    info: dict[str, Any] = {}
    pure, _ = _graph_base(st, conn, run, info, live=False)
    if api_cache.disk_only():  # integration (api-perf c90c0a3): warm-up of older runs builds the disk base only
        return
    reviews = _pair_reviews(conn)
    key = _memo_key("overview", run, info, reviews, levels="kit,campaign,review", min_bits=0.0, min_pairs=1)
    if key is not None:
        api_cache.memo_response(_GZIP_CLIENT, key, lambda: _overview_live(conn, pure, reviews,
                                                                         "kit,campaign,review", 0.0, 1))
    base, _ = _graph_base(st, conn, run, info)
    key = _memo_key("explore", run, info, reviews, top=3, levels=None, min_bits=0.0)
    if key is not None:
        api_cache.memo_response(_GZIP_CLIENT, key, lambda: _explore_payload(
            run, {"corpus": base.raw["corpus"], "synthetic": base.raw["synthetic"]},
            _explore_data(conn, base, reviews, 3)))


def _edge_dict(e: list[Any], top: int) -> dict[str, Any]:
    """Compact overlay row -> the verbose /explore edge object (`unsure` is not a must/cannot-link there)."""
    rob = e[lod.E_ROB]
    return {
        "id": f"{e[0]}|{e[1]}", "source": e[0], "target": e[1], "bits": e[2], "p": e[3], "hypothesis": e[4],
        "level": e[5], "family": e[6], "family_bits": e[7], "campaign_bits": e[8], "kit_bits": e[9],
        "asset_bits": e[10], "inferred": e[11],
        "robustness": {"robust": rob[0], "critical": rob[1], "link": rob[2]} if rob else None,
        "review": e[13] if e[13] in ("must_link", "cannot_link") else None,
        "reasons": [{"family": r[0], "kind": r[1], "bits": r[2], "value": r[3], "doc_freq": r[4], "text": r[5]}
                    for r in e[14][:top]],
    }


def _keep(levels: str | None, min_bits: float) -> Any:
    allowed = {x for x in (levels or "").split(",") if x}
    return lambda e: (not allowed or e[lod.E_LEVEL] in allowed) and e[lod.E_BITS] >= min_bits


def _explore_data(conn: sqlite3.Connection, base: lod.GraphBase, reviews: dict[tuple[str, str], str], top: int,
                  levels: str | None = None, min_bits: float = 0.0) -> dict[str, Any]:
    rows, extra = base.overlay(conn, reviews)
    keep = _keep(levels, min_bits)
    edges = [_edge_dict(e, top) for e in rows if keep(e)]
    degree = Counter([e["source"] for e in edges] + [e["target"] for e in edges])
    node_rows = sorted([*base.nodes, *extra.values()], key=lambda n: n[lod.N_ID])
    nodes = [{"id": n[0], "title": n[1], "campaign_id": n[2], "group_id": n[3], "observed_at": n[4],
              "registered_at": n[5], "corpus": n[6], "thumbnail": n[7], "degree": degree[n[0]]} for n in node_rows]
    return {"members": {n["id"]: n["campaign_id"] for n in nodes if n["campaign_id"]},
            "campaigns": base.campaigns, "groups": base.groups, "edges": edges, "nodes": nodes}


@router.get("/explore")
def explore(request: Request, run_id: str | None = None, top_reasons: int = Query(3, ge=0, le=12),
            levels: str | None = Query(None, description="comma list of edge levels to keep (default all)"),
            min_bits: float = Query(0.0, ge=0.0)) -> Any:
    """Everything the graph explorer draws, in one payload (independent pairs are left out: they are not links).
    Legacy full payload; the level-of-detail endpoints below serve the same data in parts (docs/ui/GRAPH_API.md)."""
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None, "nodes": [], "edges": [], "campaigns": [], "groups": [], "stats": None}
        if top_reasons > lod.BASE_TOP:  # rare: more reasons than the cached base keeps; uncached legacy path
            hit = False
            prow = conn.execute("SELECT params FROM runs WHERE id=?", (run,)).fetchone()
            params = (_json(prow["params"], {}) or {}) if prow is not None else {}
            params = corpus_params(params, conn)
            meta = {"corpus": run_corpus(params, conn), "synthetic": bool(params.get("synthetic"))}
            data = _load(conn, run, top=top_reasons)
            allowed = {x for x in (levels or "").split(",") if x}
            data["edges"] = [e for e in data["edges"]
                             if (not allowed or e["level"] in allowed) and e["bits"] >= min_bits]
        else:
            info: dict[str, Any] = {}
            base, hit = _graph_base(st, conn, run, info)
            reviews = _pair_reviews(conn)

            def build() -> dict[str, Any]:
                data = _explore_data(conn, base, reviews, top_reasons, levels, min_bits)
                return _explore_payload(run, {"corpus": base.raw["corpus"], "synthetic": base.raw["synthetic"]},
                                        data)

            key = _memo_key("explore", run, info, reviews, top=top_reasons, levels=levels, min_bits=min_bits)
            return _respond(request, key, build, hit)
    return api_cache.json_response(_explore_payload(run, meta, data), hit=hit)


def _explore_payload(run: str, meta: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    nodes, edges = data["nodes"], data["edges"]
    camps = sorted(data["campaigns"].values(), key=lambda c: (-c["size"], c["id"]))
    return {
        "run_id": run, **meta, "thresholds": {"link_bits": LINK_BITS, "kit_bits": KIT_BITS},
        "nodes": nodes, "edges": edges, "campaigns": camps, "groups": data["groups"],
        "stats": _stats([n["id"] for n in nodes], edges, camps, len(data["groups"])),
    }


# --------------------------------------------------------------------------------------- level of detail
# docs/ui/GRAPH_API.md. All endpoints read the cached per-run base and overlay review state per request.


def _campaign_info(base: lod.GraphBase, cid: str) -> dict[str, Any]:
    c = base.campaigns.get(cid)
    disc = base.layout["campaigns"].get(cid) or [None, None, None]
    if c is None:  # solo:<incident>
        n = base.node_by_id.get(cid[5:]) if cid.startswith("solo:") else None
        c = {"id": cid, "label": (n[lod.N_TITLE] if n else None) or cid, "confidence": None, "group_id": None,
             "first_seen": n[lod.N_OBS] if n else None, "last_seen": n[lod.N_OBS] if n else None, "scheme": None}
    return {**c, "size": len(base.by_campaign.get(cid, [])), "x": disc[0], "y": disc[1], "r": disc[2]}


def _overview_payload(conn: sqlite3.Connection, base: lod.GraphBase, reviews: dict[tuple[str, str], str],
                      levels: str, min_bits: float, min_pairs: int) -> dict[str, Any]:
    rows, extra = base.overlay(conn, reviews)
    allowed = {x for x in levels.split(",") if x}
    internal: dict[str, list[Any]] = {}
    links: dict[tuple[str, str], list[Any]] = {}
    for e in rows:
        ca, cb = base.campaign_of(e[0]), base.campaign_of(e[1])
        if ca == cb:
            cur = internal.setdefault(ca, [0, 0.0, Counter()])
            cur[0] += 1
            cur[1] = max(cur[1], e[lod.E_BITS])
            for f, v in (e[lod.E_FB] or {}).items():
                if v > 0:
                    cur[2][f] += v
            continue
        # analyst cannot-links never join two campaigns on the map
        if e[lod.E_LEVEL] not in allowed or e[lod.E_BITS] < min_bits or e[13] == "cannot_link":
            continue
        k = (ca, cb) if ca < cb else (cb, ca)
        cur = links.get(k)
        if cur is None:
            cur = links[k] = [0, 0.0, 0.0, Counter(), e[lod.E_LEVEL]]
        cur[0] += 1
        cur[1] = max(cur[1], e[lod.E_BITS])
        cur[2] += e[lod.E_BITS]
        if e[lod.E_FAM]:
            cur[3][e[lod.E_FAM]] += e[lod.E_BITS]
        if lod.LEVEL_RANK.get(e[lod.E_LEVEL], 0) > lod.LEVEL_RANK.get(cur[4], 0):
            cur[4] = e[lod.E_LEVEL]
    camp_rows = []
    for cid, mids in sorted(base.by_campaign.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        info = _campaign_info(base, cid)
        it = internal.get(cid)
        thumb = next((base.node_by_id[m][lod.N_THUMB] for m in mids if base.node_by_id[m][lod.N_THUMB]), None)
        camp_rows.append([cid, info["label"], info["confidence"], info["group_id"], info["size"], info["first_seen"],
                          info["last_seen"], info["scheme"], info["x"], info["y"], info["r"], it[0] if it else 0,
                          round(it[1], 3) if it else None, it[2].most_common(1)[0][0] if it and it[2] else None,
                          thumb, cid not in base.campaigns])
    group_rows = []
    for g in base.groups:
        disc = base.layout["groups"].get(g["id"])
        if disc is None:
            continue
        cids = [c for c in g.get("campaign_ids") or [] if c in base.by_campaign]
        group_rows.append([g["id"], g.get("label") or g["id"], len(cids),
                           sum(len(base.by_campaign[c]) for c in cids), *disc])
    discs = list(base.layout["campaigns"].values()) + list(base.layout["groups"].values())
    bounds = {"x0": min((d[0] - d[2] for d in discs), default=0), "y0": min((d[1] - d[2] for d in discs), default=0),
              "x1": max((d[0] + d[2] for d in discs), default=0), "y1": max((d[1] + d[2] for d in discs), default=0)}
    link_rows = [[s, t, v[0], v[1], round(v[2], 3), v[3].most_common(1)[0][0] if v[3] else None, v[4]]
                 for (s, t), v in sorted(links.items(), key=lambda kv: (-kv[1][1], kv[0])) if v[0] >= min_pairs]
    node_ids = sorted([*base.node_by_id, *extra])
    camps = sorted(base.campaigns.values(), key=lambda c: (-c["size"], c["id"]))
    return {
        "run_id": base.run_id, "corpus": base.raw["corpus"], "synthetic": base.raw["synthetic"],
        "thresholds": {"link_bits": LINK_BITS, "kit_bits": KIT_BITS},
        "stats": _stats_rows(node_ids, rows, camps, len(base.groups)), "bounds": bounds,
        "groups": lod.table(["id", "label", "n_campaigns", "size", "x", "y", "r"], group_rows),
        "campaigns": lod.table(["id", "label", "confidence", "group_id", "size", "first_seen", "last_seen", "scheme",
                                "x", "y", "r", "internal_edges", "max_internal_bits", "family", "thumbnail", "solo"],
                               camp_rows),
        "links": lod.table(["source", "target", "pairs", "max_bits", "sum_bits", "family", "level"], link_rows),
    }


@router.get("/overview")
def overview(request: Request, run_id: str | None = None,
             levels: str = Query("kit,campaign,review", description="cross-campaign pair levels aggregated to links"),
             min_bits: float = Query(0.0, ge=0.0), min_pairs: int = Query(1, ge=1)) -> Any:
    """First paint of the explorer: campaign / group discs with server-side layout and campaign-to-campaign links."""
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None, "stats": None, "bounds": None, "groups": lod.table([], []),
                    "campaigns": lod.table([], []), "links": lod.table([], [])}
        info: dict[str, Any] = {}
        base, hit = _graph_base(st, conn, run, info, live=False)  # new material: dataflow.overview_view
        reviews = _pair_reviews(conn)
        key = _memo_key("overview", run, info, reviews, levels=levels, min_bits=min_bits, min_pairs=min_pairs)
        return _respond(request, key, lambda: _overview_live(conn, base, reviews, levels, min_bits, min_pairs),
                        hit)


def _overview_live(conn: sqlite3.Connection, base: lod.GraphBase, reviews: dict[tuple[str, str], str],
                   levels: str, min_bits: float, min_pairs: int) -> dict[str, Any]:
    """API-PERF overview of the base + DATAFLOW live layer (docs/ui/STREAM_API.md) when aggregates are ready."""
    from hydra import dataflow

    payload = _overview_payload(conn, base, reviews, levels, min_bits, min_pairs)
    if base.raw.get("scope") == "base" and dataflow.is_ready(conn, base.run_id):
        return dataflow.overview_view(conn, base.run_id, payload, levels, min_bits, min_pairs)
    return {**payload, "levels": levels, "min_bits": min_bits, "live": dataflow.live_info(conn, base.run_id)}


def _node_lookup(base: lod.GraphBase, extra: dict[str, list[Any]]) -> Any:
    return lambda nid: base.node_by_id.get(nid) or extra.get(nid)


@router.get("/campaign/{campaign_id}")
def campaign_expand(request: Request, campaign_id: str, run_id: str | None = None,
                    top_reasons: int = Query(3, ge=0, le=lod.BASE_TOP), external: bool = True,
                    external_limit: int = Query(300, ge=0, le=5000), min_bits: float = Query(0.0, ge=0.0)) -> Any:
    """Members, internal links and links leaving the expanded campaign(s) (comma-separated ids, max 50)."""
    cids = [c for c in dict.fromkeys(campaign_id.split(",")) if c]
    if not cids or len(cids) > 50:
        raise HTTPException(status_code=400, detail="1-50 campaign ids")
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        base, hit = _graph_base(st, conn, run)
        missing = [c for c in cids if c not in base.by_campaign]
        if missing:
            raise HTTPException(status_code=404, detail=f"campaign not in run: {', '.join(missing[:5])}")
        rows, extra = base.overlay(conn, _pair_reviews(conn))
    inside = {m for c in cids for m in base.by_campaign[c]}
    degree = lod.degree_of(rows)
    edges, ext = [], []
    for e in rows:
        a_in, b_in = e[0] in inside, e[1] in inside
        if a_in and b_in:
            edges.append(lod.edge_out(e, top_reasons))
        elif (a_in or b_in) and external and e[lod.E_BITS] >= min_bits:
            ext.append(e)
    ext.sort(key=lambda e: -e[lod.E_BITS])
    kept = ext[:external_limit]
    far = sorted({x for e in kept for x in (e[0], e[1]) if x not in inside})
    node = _node_lookup(base, extra)
    return api_cache.json_response({
        "run_id": run, "campaigns": [_campaign_info(base, c) for c in cids],
        "nodes": lod.table(lod.NODE_COLUMNS, [lod.node_out(node(m), degree, base.layout) for m in sorted(inside)]),
        "edges": lod.table(lod.EDGE_COLUMNS, edges),
        "external": lod.table(lod.EDGE_COLUMNS, [lod.edge_out(e, top_reasons) for e in kept]),
        "external_nodes": lod.table(lod.NODE_COLUMNS, [lod.node_out(node(x), degree, base.layout) for x in far
                                                       if node(x) is not None]),
        "truncated": {"external": len(ext) - len(kept)},
    }, hit=hit)


@router.get("/neighbors/{incident_id}")
def neighbors(request: Request, incident_id: str, run_id: str | None = None, min_bits: float = Query(0.0, ge=0.0),
              levels: str | None = None, limit: int = Query(200, ge=1, le=5000),
              top_reasons: int = Query(3, ge=0, le=lod.BASE_TOP)) -> Any:
    """Ego network of one incident: its links (strongest first) and the incidents at the other end."""
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        base, hit = _graph_base(st, conn, run)
        rows, extra = base.overlay(conn, _pair_reviews(conn))
    node = _node_lookup(base, extra)
    me = node(incident_id)
    if me is None:
        raise HTTPException(status_code=404, detail="incident not in this run")
    keep = _keep(levels, min_bits)
    idx = base.adj.get(incident_id, []) + [i for i in range(len(base.edges), len(rows))
                                           if incident_id in (rows[i][0], rows[i][1])]
    mine = sorted((rows[i] for i in idx if keep(rows[i])), key=lambda e: -e[lod.E_BITS])
    kept = mine[:limit]
    degree = lod.degree_of(rows)
    far = sorted({e[1] if e[0] == incident_id else e[0] for e in kept})
    return api_cache.json_response({
        "run_id": run, "incident": dict(zip(lod.NODE_COLUMNS, lod.node_out(me, degree, base.layout), strict=True)),
        "nodes": lod.table(lod.NODE_COLUMNS, [lod.node_out(node(x), degree, base.layout) for x in far
                                              if node(x) is not None]),
        "edges": lod.table(lod.EDGE_COLUMNS, [lod.edge_out(e, top_reasons) for e in kept]),
        "total": len(mine), "truncated": len(mine) - len(kept),
    }, hit=hit)


@router.get("/viewport")
def viewport(request: Request, x0: float, y0: float, x1: float, y1: float, run_id: str | None = None,
             limit: int = Query(1500, ge=1, le=20000), min_bits: float = Query(0.0, ge=0.0),
             max_edges: int = Query(5000, ge=0, le=100000), top_reasons: int = Query(0, ge=0, le=lod.BASE_TOP)) -> Any:
    """Incidents whose layout position is inside the box (highest degree first) and the links among them."""
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None, "nodes": lod.table(lod.NODE_COLUMNS, []), "edges": lod.table(lod.EDGE_COLUMNS, []),
                    "truncated": {"nodes": 0, "edges": 0}}
        base, hit = _graph_base(st, conn, run)
        rows, _extra = base.overlay(conn, _pair_reviews(conn))
    lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    degree = lod.degree_of(rows)
    pos = base.layout["nodes"]
    inside = [n for n in base.nodes
              if (xy := pos.get(n[lod.N_ID])) and lo_x <= xy[0] <= hi_x and lo_y <= xy[1] <= hi_y]
    inside.sort(key=lambda n: (-degree[n[lod.N_ID]], n[lod.N_ID]))
    kept = inside[:limit]
    ids = {n[lod.N_ID] for n in kept}
    es = sorted((e for e in rows if e[0] in ids and e[1] in ids and e[lod.E_BITS] >= min_bits),
                key=lambda e: -e[lod.E_BITS])
    return api_cache.json_response({
        "run_id": run,
        "nodes": lod.table(lod.NODE_COLUMNS, [lod.node_out(n, degree, base.layout)
                                              for n in sorted(kept, key=lambda n: n[lod.N_ID])]),
        "edges": lod.table(lod.EDGE_COLUMNS, [lod.edge_out(e, top_reasons) for e in es[:max_edges]]),
        "truncated": {"nodes": len(inside) - len(kept), "edges": max(0, len(es) - max_edges)},
    }, hit=hit)


@router.get("/edge/{a}/{b}")
def edge_detail(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    """One link in full (all reasons, bit waterfall, review), read by primary key; also for independent pairs."""
    if a == b:
        raise HTTPException(status_code=400, detail="pick two different incidents")
    lo, hi = sorted((a, b))
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        r = conn.execute("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=? AND a=?"
                         " AND b=?", (run, lo, hi)).fetchone()
        if r is None:
            raise HTTPException(status_code=404, detail="pair not scored in this run")
        review = _pair_reviews(conn).get((lo, hi))
        camp = {x["incident_id"]: x["campaign_id"] for x in conn.execute(
            "SELECT incident_id, campaign_id FROM campaign_members WHERE run_id=? AND incident_id IN (?, ?)",
            (run, lo, hi))}
    detail = lod.loads(r["detail"])
    e = lod.edge_from_row(r["a"], r["b"], r["total_bits"], r["probability"], r["hypothesis"], detail, top=0)
    if e[lod.E_LEVEL] is None and review == "accepted":
        e[lod.E_LEVEL] = "review"
    reasons = sorted([x for x in detail.get("reasons") or [] if isinstance(x, dict)],
                     key=lambda x: -(x.get("bits") or 0))
    out_reasons, water, cum = [], [], 0.0
    for x in reasons:
        bits = round(float(x.get("bits") or 0), 3)
        cum += bits
        out_reasons.append({"family": x.get("family"), "kind": x.get("kind"), "bits": bits,
                            "value_a": x.get("value_a"), "value_b": x.get("value_b"), "doc_freq": x.get("doc_freq"),
                            "text": x.get("explanation_pl") or x.get("kind")})
        water.append({"family": x.get("family"), "kind": x.get("kind"), "bits": bits, "cumulative": round(cum, 3)})
    rob = e[lod.E_ROB]
    return {"run_id": run, "source": lo, "target": hi, "bits": e[lod.E_BITS], "p": e[lod.E_P],
            "hypothesis": e[lod.E_HYP], "level": e[lod.E_LEVEL], "family": e[lod.E_FAM], "family_bits": e[lod.E_FB],
            "campaign_bits": e[lod.E_CB], "kit_bits": e[lod.E_KB], "asset_bits": e[lod.E_AB],
            "inferred": e[lod.E_INF],
            "robustness": {"robust": rob[0], "critical": rob[1], "link": rob[2]} if rob else None,
            "review": lod.REVIEW_OF.get(review or ""),
            "missing_families": detail.get("missing_families") or [],
            "contradictions": detail.get("contradictions") or [],
            "campaigns": {"a": camp.get(lo), "b": camp.get(hi)}, "reasons": out_reasons, "waterfall": water}


@router.get("/path")
def evidence_path(request: Request, a: str, b: str, run_id: str | None = None,
                  levels: str = Query("campaign,kit", description="edge levels the path may use")) -> dict[str, Any]:
    """Strongest evidence path: Dijkstra over campaign/kit links (and analyst must-links), never across a
    cannot-link. Hop cost = 1 + 20/bits, so fewer hops win first and stronger links break ties."""
    allowed = {x for x in levels.split(",") if x} | {"review"}
    if a == b:
        raise HTTPException(status_code=400, detail="pick two different incidents")
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        base, _hit = _graph_base(st, conn, run)
        rows, _extra = base.overlay(conn, _pair_reviews(conn))
        adj: dict[str, list[tuple[str, float, dict[str, Any]]]] = defaultdict(list)
        for row in rows:
            if row[13] == "cannot_link" or row[lod.E_LEVEL] not in allowed or row[lod.E_BITS] <= 0:
                continue
            e = _edge_dict(row, 5)
            cost = 1.0 + 20.0 / max(e["bits"], 0.5)
            adj[e["source"]].append((e["target"], cost, e))
            adj[e["target"]].append((e["source"], cost, e))
        dist = {a: 0.0}
        prev: dict[str, tuple[str, dict[str, Any]]] = {}
        heap = [(0.0, a)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == b:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, c, e in adj.get(u, []):
                nd = d + c
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = (u, e)
                    heapq.heappush(heap, (nd, v))
        members = base.members
        if b not in prev:
            return {"run_id": run, "a": a, "b": b, "found": False, "nodes": [], "steps": [],
                    "reason_pl": "Brak ścieżki dowodowej na poziomie kampanii / zestawu między tymi incydentami."}
        steps = []
        cur = b
        while cur != a:
            p, e = prev[cur]
            steps.append((p, cur, e))
            cur = p
        steps.reverse()
        titles = {r["id"]: r["title"] for r in conn.execute(
            f"SELECT id, title FROM incidents WHERE id IN ({_in([a] + [s[1] for s in steps])})",
            [a] + [s[1] for s in steps])}
    path_nodes = [a] + [s[1] for s in steps]
    out_steps = []
    for frm, to, e in steps:
        out_steps.append({"from": frm, "to": to, "edge_id": e["id"], "bits": e["bits"], "level": e["level"],
                          "level_pl": LEVEL_PL.get(e["level"], e["level"]), "family": e["family"],
                          "review": e["review"], "genes": e["reasons"][:5]})
    from hydra.labels_pl import num_pl

    weakest = min(out_steps, key=lambda s: s["bits"])
    return {"run_id": run, "a": a, "b": b, "found": True, "hops": len(out_steps),
            "nodes": [{"id": n, "title": titles.get(n), "campaign_id": members.get(n)} for n in path_nodes],
            "steps": out_steps, "weakest_bits": weakest["bits"],
            "summary_pl": f"{len(out_steps)} krok(i), najsłabsze ogniwo {num_pl(weakest['bits'])} b"
                          f" ({weakest['level_pl']})"}


@router.get("/indicators")
def indicators(request: Request, min_df: int = Query(2, ge=2, le=1000), kinds: str | None = None,
               limit: int = Query(400, ge=1, le=5000)) -> dict[str, Any]:
    """Incidents <-> shared indicator values. `over_correlating` marks values the genome tiering put on a
    boilerplate / label tier or that matched a MISP warninglist (shared platforms, CDNs, parking): they
    connect many unrelated incidents and carry ~0 bits."""
    wanted = tuple(k for k in (kinds.split(",") if kinds else INDICATOR_KINDS) if k)
    with _st(request).reader() as conn:
        run = resolve_run(conn)
        members = _membership(conn, run)
        scope = run_incidents(conn, run)
        if not scope:
            return {"run_id": run, "incidents_total": 0, "indicators": [], "kinds": list(wanted)}
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _g_scope (id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _g_scope")
        conn.executemany("INSERT OR IGNORE INTO _g_scope VALUES (?)", [(s,) for s in scope])
        rows = conn.execute(
            "SELECT MIN(family) AS family, kind, value, COUNT(DISTINCT incident_id) AS df,"
            " GROUP_CONCAT(DISTINCT incident_id) AS ids,"
            " MAX(CASE WHEN dependency_group LIKE '%:boilerplate:%' OR dependency_group LIKE '%:label:%'"
            "     OR evidence LIKE '%warninglist:%' THEN 1 ELSE 0 END) AS noisy,"
            " MAX(dependency_group) AS dep"
            f" FROM genes WHERE kind IN ({_in(list(wanted))}) AND status='observed' AND value IS NOT NULL"
            " AND incident_id IN (SELECT id FROM _g_scope) GROUP BY kind, value"
            " HAVING df >= ? ORDER BY df DESC, kind, value LIMIT ?", (*wanted, min_df, limit)).fetchall()
        n_inc = len(scope)
    items = []
    for r in rows:
        share = r["df"] / max(1, n_inc)
        reason = None
        if r["noisy"]:
            reason = "lista ostrzeżeń MISP / poziom szablonowy (boilerplate)"
        elif len((r["value"] or "").strip("/ ")) <= 1:
            reason = "wartość trywialna (np. pusta ścieżka „/”)"
        elif share > 0.5 or (share > 0.2 and n_inc >= 20):
            reason = f"występuje w {share:.0%} incydentów korpusu"
        items.append({"id": f"{r['kind']}:{r['value']}", "family": r["family"], "kind": r["kind"],
                      "kind_pl": INDICATOR_KIND_PL.get(r["kind"], r["kind"]), "value": r["value"][:160],
                      "df": r["df"], "tier": r["dep"], "over_correlating": reason is not None, "reason_pl": reason,
                      "incidents": sorted((r["ids"] or "").split(","))})
    used = sorted({i for it in items for i in it["incidents"]})
    return {"run_id": run, "incidents_total": n_inc, "kinds": list(wanted), "indicators": items,
            "incidents": [{"id": i, "campaign_id": members.get(i)} for i in used]}


class ExportIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=20000)
    format: Literal["json", "cypher"] = "json"
    run_id: str | None = None




@router.post("/export")
def export_subgraph(request: Request, body: ExportIn) -> Response:
    """The selected incidents, their campaign membership and every evidence link between them."""
    keep = set(body.ids)
    with _st(request).reader() as conn:
        run = resolve_run(conn, body.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        base, _hit = _graph_base(_st(request), conn, run)
        data = _explore_data(conn, base, _pair_reviews(conn), 5)
    nodes = [n for n in data["nodes"] if n["id"] in keep]
    edges = [e for e in data["edges"] if e["source"] in keep and e["target"] in keep]
    cids = sorted({n["campaign_id"] for n in nodes if n["campaign_id"]})
    camps = [data["campaigns"][c] for c in cids if c in data["campaigns"]]
    record_export(_st(request).writer, f"subgraph_{body.format}", run,
                  {"incidents": len(nodes), "edges": len(edges), "campaigns": len(camps)})
    if body.format == "json":
        payload = redact_obj({"run_id": run, "nodes": nodes, "edges": edges, "campaigns": camps})
        return Response(json.dumps(payload, ensure_ascii=False, indent=1, default=str), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="hydra-subgraph.json"'})
    # B19 / CR-VP-E2: the same graph model as `hydra export-neo4j` and `GET /v1/export/neo4j` (Gene nodes,
    # HAS_GENE, LINKED_TO), restricted to the selection, so the documented queries of docs/NEO4J.md run on it
    from hydra.export_neo4j import build_graph, inline_cypher, subgraph

    with _st(request).reader() as conn:
        text = inline_cypher(subgraph(build_graph(conn, run), keep))
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="hydra-subgraph.cypher"'})


FAMILY_PL = {"VIS": "wizualne", "TXT": "tekstowe", "INF": "infrastruktura", "ID": "tożsamość", "NAR": "narracja",
             "META": "metadane"}


@router.get("/campaigns/{campaign_id}/summary")
def campaign_summary(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    """Rule-based 'why this cluster' text from the campaign card and its internal links (no LLM)."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        card = _campaign_card(conn, run, campaign_id)
        members = set(card.get("members") or [])
        reviews = _pair_reviews(conn)
        internal = []
        if len(members) > 1:
            ids = sorted(members)
            for r in conn.execute(
                "SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=?"
                f" AND a IN ({_in(ids)}) AND b IN ({_in(ids)})", (run, *ids, *ids)):
                if (e := _edge_row(r, reviews, 12)) is not None:
                    internal.append(e)
    fam = Counter()
    for e in internal:
        for f, v in e["family_bits"].items():
            if v > 0:
                fam[f] += v
    core = [g for g in card.get("core_genome") or [] if isinstance(g, dict)][:5]
    levels = Counter(e["level"] for e in internal)
    rob = [e["robustness"] for e in internal if e["robustness"]]
    lines = [f"Kampania {campaign_id} „{card.get('label')}” ma {len(members)} incydent(ów)."]
    if card.get("first_seen") or card.get("last_seen"):
        lines.append(f"Obserwowana od {card.get('first_seen') or '?'} do {card.get('last_seen') or '?'}.")
    if internal:
        lines.append(f"Łączy ją {len(internal)} powiązań wewnętrznych: "
                     + ", ".join(f"{LEVEL_PL.get(k, k)} {v}" for k, v in levels.most_common()) + ".")
    else:
        lines.append("Nie ma powiązań wewnętrznych (incydent pojedynczy albo łączony tylko decyzją analityka).")
    if fam:
        lines.append("Dominujące rodziny dowodów: " + ", ".join(
            f"{f} {FAMILY_PL.get(f, f)} ({v:.0f} b)" for f, v in fam.most_common(3)) + ".")
    if rob:
        n_rob = sum(1 for x in rob if x["robust"])
        crit = Counter(c for x in rob for c in x["critical"])
        line = f"Odporność: {n_rob} z {len(rob)} powiązań utrzymuje się bez każdej pojedynczej rodziny."
        if crit:
            line += " Krytyczne rodziny: " + ", ".join(f"{k} ({v})" for k, v in crit.most_common(3)) + "."
        lines.append(line)
    else:
        lines.append("Odporność powiązań nie została oceniona w tym przebiegu.")
    return {"run_id": run, "campaign_id": campaign_id, "status": "rule_based", "text_pl": " ".join(lines),
            "core_genes": [{"family": g.get("family"), "kind": g.get("kind"), "bits": g.get("bits"),
                            "text": g.get("explanation_pl")} for g in core],
            "family_bits": {k: round(v, 1) for k, v in fam.most_common()}, "levels": dict(levels)}
