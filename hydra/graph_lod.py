"""Per-run graph base for the level-of-detail graph API (docs/ui/GRAPH_API.md).

`build_base` reads every non-independent pair of a run once (the expensive part: `pairs.detail` JSON), keeps the
compact edge rows the explorer draws, the incident nodes, campaigns, groups and a deterministic layout. The base
holds no review state; `GraphBase.overlay` adds analyst decisions per request. Every number is copied from
`pairs` / `campaigns` / `genes`; nothing is estimated.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from typing import Any

import orjson

BASE_TOP = 5  # reasons kept per edge in the base; explore / path / export never need more than 5
LEVEL_RANK = {"review": 5, "campaign": 4, "kit": 3, "asset": 2, "weak": 1}

NODE_COLUMNS = ["id", "title", "campaign_id", "group_id", "observed_at", "registered_at", "corpus", "thumbnail",
                "degree", "x", "y"]
EDGE_COLUMNS = ["source", "target", "bits", "p", "hypothesis", "level", "family", "family_bits", "campaign_bits",
                "kit_bits", "asset_bits", "inferred", "robust", "review", "reasons"]
# base edge row: EDGE_COLUMNS without `review`; reasons as [family, kind, bits, value, doc_freq, text]
E_SRC, E_TGT, E_BITS, E_P, E_HYP, E_LEVEL, E_FAM, E_FB, E_CB, E_KB, E_AB, E_INF, E_ROB, E_REASONS = range(14)
# base node row
N_ID, N_TITLE, N_CAMP, N_GROUP, N_OBS, N_REG, N_CORPUS, N_THUMB = range(8)
REVIEW_OF = {"accepted": "must_link", "rejected": "cannot_link", "unsure": "unsure"}


def loads(text: Any) -> Any:
    if not text:
        return {}
    try:
        return orjson.loads(text)
    except orjson.JSONDecodeError:
        return {}


def edge_from_row(a: str, b: str, total_bits: float, probability: float, hypothesis: str, detail: dict[str, Any],
                  top: int = BASE_TOP) -> list[Any]:
    """Compact edge (base row) with the same rules as api_graph._edge_row; level None = not a link by itself."""
    from hydra.api_graph import _inferred, _level, _robust
    from hydra.api_task import _dominant_family

    fam_bits = detail.get("family_bits") or {}
    reasons = [x for x in detail.get("reasons") or [] if isinstance(x, dict)]
    rob = _robust(detail)
    top_r = sorted(reasons, key=lambda x: -(x.get("bits") or 0))[:top]
    return [a, b, round(float(total_bits), 3), probability, hypothesis, _level(hypothesis, detail),
            _dominant_family(fam_bits), {k: round(float(v), 2) for k, v in fam_bits.items() if v},
            detail.get("campaign_bits"), detail.get("kit_bits"), detail.get("asset_bits"), _inferred(reasons),
            [rob["robust"], rob["critical"], rob["link"]] if rob else None,
            [[r.get("family"), r.get("kind"), round(float(r.get("bits") or 0), 2),
              (r.get("value_a") or r.get("value_b") or "")[:120] or None, r.get("doc_freq"),
              r.get("explanation_pl") or r.get("kind")] for r in top_r]]


def node_rows(conn: sqlite3.Connection, ids: list[str], members: dict[str, str],
              camps: dict[str, dict[str, Any]]) -> list[list[Any]]:
    from hydra.api_graph import DATE_RE, _corpus_of, _thumbs
    from hydra.api_task import _in

    reg: dict[str, str] = {}
    inc: dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for r in conn.execute(
            "SELECT incident_id, MIN(value) AS v FROM genes WHERE kind='registration_day' AND status='observed'"
            f" AND incident_id IN ({_in(chunk)}) GROUP BY incident_id", chunk):
            if r["v"] and DATE_RE.match(r["v"]):
                reg[r["incident_id"]] = r["v"][:10]
        inc.update({r["id"]: r for r in conn.execute(
            f"SELECT id, title, observed_at, source FROM incidents WHERE id IN ({_in(chunk)})", chunk)})
    thumbs = _thumbs(conn, ids)
    out = []
    for n in ids:
        row = inc.get(n)
        cid = members.get(n)
        obs = row["observed_at"] if row is not None and row["observed_at"] else None
        out.append([n, row["title"] if row is not None else None, cid,
                    camps[cid]["group_id"] if cid in camps else None,
                    obs[:10] if obs and DATE_RE.match(obs) else None, reg.get(n),
                    _corpus_of(row["source"] if row is not None else None), thumbs.get(n)])
    return out


def build_base(conn: sqlite3.Connection, run: str, live: bool = True) -> dict[str, Any]:
    from hydra.api_task import _groups, _json, _membership, corpus_params, run_corpus, run_incidents

    prow = conn.execute("SELECT params FROM runs WHERE id=?", (run,)).fetchone()
    params = corpus_params((_json(prow["params"], {}) or {}) if prow is not None else {}, conn)
    members = _membership(conn, run)
    camps: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT id, label, confidence, detail FROM campaigns WHERE run_id=?", (run,)):
        d = loads(r["detail"])
        camps[r["id"]] = {"id": r["id"], "label": r["label"], "confidence": r["confidence"],
                          "group_id": d.get("group_id"), "first_seen": d.get("first_seen"),
                          "last_seen": d.get("last_seen"), "scheme": d.get("scheme"), "size": 0}
    groups = _groups(conn, run)
    group_of = {cid: g["id"] for g in groups for cid in (g.get("campaign_ids") or [])}
    for c in camps.values():
        c["group_id"] = c["group_id"] or group_of.get(c["id"])
    edges = [edge_from_row(r[0], r[1], r[2], r[3], r[4], loads(r[5])) for r in conn.execute(
        "SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs"
        " WHERE run_id=? AND hypothesis!='independent' ORDER BY total_bits DESC", (run,))]
    ids = sorted(set(run_incidents(conn, run, live=live)) | {x for e in edges for x in (e[E_SRC], e[E_TGT])})
    nodes = node_rows(conn, ids, members, camps)
    for n in nodes:
        if n[N_CAMP] in camps:
            camps[n[N_CAMP]]["size"] += 1
    layout = pack_layout(nodes, camps, groups)
    return {"run_id": run, "corpus": run_corpus(params, conn), "synthetic": bool(params.get("synthetic")),
            "nodes": nodes, "edges": edges, "campaigns": camps, "groups": groups, "layout": layout}


# --------------------------------------------------------------------------------------- layout
# Same deterministic hierarchy as ui/src/components/graph/layout.ts (packLayout): incidents on a sunflower inside
# their campaign disc, campaign discs spiral-packed inside their group disc, groups + ungrouped campaigns at the top.

GOLDEN = math.pi * (3 - math.sqrt(5))


def sunflower(n: int, s: float = 1.0) -> tuple[list[tuple[float, float]], float]:
    pts = []
    for i in range(n):
        rr = 0.0 if n == 1 else s * math.sqrt(i + 0.5) * 0.9
        pts.append((rr * math.cos(i * GOLDEN), rr * math.sin(i * GOLDEN)))
    return pts, (s * 0.8 if n <= 1 else s * math.sqrt(n) * 0.9 + s * 0.8)


def spiral_pack(items: list[tuple[str, float]], gap: float = 0.6) -> tuple[dict[str, tuple[float, float]], float]:
    ordered = sorted(items, key=lambda it: (-it[1], it[0]))
    at: dict[str, tuple[float, float]] = {}
    if not ordered:
        return at, 0.0
    cell = max(1.0, ordered[-1][1] * 2 + gap)
    grid: dict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)
    placed: list[tuple[float, float, float]] = []

    def fits(x: float, y: float, r: float) -> bool:
        ext = r + gap
        for i in range(math.floor((x - ext) / cell), math.floor((x + ext) / cell) + 1):
            for j in range(math.floor((y - ext) / cell), math.floor((y + ext) / cell) + 1):
                for px, py, pr in grid.get((i, j), ()):
                    if math.hypot(px - x, py - y) < pr + r + gap:
                        return False
        return True

    def add(pid: str, x: float, y: float, r: float) -> None:
        p = (x, y, r)
        placed.append(p)
        at[pid] = (x, y)
        for i in range(math.floor((x - r) / cell), math.floor((x + r) / cell) + 1):
            for j in range(math.floor((y - r) / cell), math.floor((y + r) / cell) + 1):
                grid[(i, j)].append(p)

    add(ordered[0][0], 0.0, 0.0, ordered[0][1])
    rr = ordered[0][1] + gap
    theta = 0.0
    for pid, r in ordered[1:]:
        rr = max(0.0, rr - r * 1.5)
        for _ in range(400000):
            x, y = rr * math.cos(theta), rr * math.sin(theta)
            if fits(x, y, r):
                add(pid, x, y, r)
                break
            d_theta = max(0.15, r * 0.5) / max(rr, r)
            theta += d_theta
            rr += ((2 * r + gap) * d_theta) / (2 * math.pi)
    x0 = min(p[0] - p[2] for p in placed)
    x1 = max(p[0] + p[2] for p in placed)
    y0 = min(p[1] - p[2] for p in placed)
    y1 = max(p[1] + p[2] for p in placed)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    radius = max(math.hypot(p[0] - cx, p[1] - cy) + p[2] for p in placed)
    return {k: (v[0] - cx, v[1] - cy) for k, v in at.items()}, radius


def pack_layout(nodes: list[list[Any]], camps: dict[str, dict[str, Any]], groups: list[dict[str, Any]]
                ) -> dict[str, Any]:
    by_campaign: dict[str, list[list[Any]]] = defaultdict(list)
    for n in nodes:
        by_campaign[n[N_CAMP] or f"solo:{n[N_ID]}"].append(n)
    local: dict[str, tuple[list[tuple[str, float, float]], float]] = {}
    for cid, lst in by_campaign.items():
        lst.sort(key=lambda n: (n[N_OBS] or n[N_REG] or "9999", n[N_ID]))
        pts, r = sunflower(len(lst))
        local[cid] = ([(n[N_ID], *pts[i]) for i, n in enumerate(lst)], r)
    group_of: dict[str, str] = {}
    for g in groups:
        for c in g.get("campaign_ids") or []:
            if c in by_campaign:
                group_of[c] = g["id"]
    for c in camps.values():
        if c.get("group_id") and c["id"] in by_campaign and c["id"] not in group_of:
            group_of[c["id"]] = c["group_id"]
    in_group: dict[str, list[str]] = defaultdict(list)
    for c, g in group_of.items():
        in_group[g].append(c)
    group_local: dict[str, tuple[dict[str, tuple[float, float]], float]] = {}
    for g, cids in in_group.items():
        at, r = spiral_pack([(c, local[c][1]) for c in cids], 1.2)
        group_local[g] = (at, r + 1.5)
    top = [(f"g:{g}", v[1]) for g, v in group_local.items()] + \
          [(f"c:{c}", local[c][1]) for c in by_campaign if c not in group_of]
    packed, _ = spiral_pack(top, 2.0)
    pos: dict[str, list[float]] = {}
    c_discs: dict[str, list[float]] = {}
    g_discs: dict[str, list[float]] = {}

    def place(cid: str, cx: float, cy: float) -> None:
        pts, r = local[cid]
        c_discs[cid] = [round(cx, 3), round(cy, 3), round(r, 3)]
        for nid, x, y in pts:
            pos[nid] = [round(cx + x, 3), round(cy + y, 3)]

    for key, (x, y) in packed.items():
        if key.startswith("g:"):
            at, r = group_local[key[2:]]
            g_discs[key[2:]] = [round(x, 3), round(y, 3), round(r, 3)]
            for cid, (px, py) in at.items():
                place(cid, x + px, y + py)
        else:
            place(key[2:], x, y)
    return {"nodes": pos, "campaigns": c_discs, "groups": g_discs}


# --------------------------------------------------------------------------------------- in-memory view


class GraphBase:
    """Loaded base plus indexes; read-only (shared across requests)."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.run_id: str = raw["run_id"]
        self.nodes: list[list[Any]] = raw["nodes"]
        self.edges: list[list[Any]] = raw["edges"]
        self.campaigns: dict[str, dict[str, Any]] = raw["campaigns"]
        self.groups: list[dict[str, Any]] = raw["groups"]
        self.layout: dict[str, Any] = raw["layout"]
        self.node_by_id = {n[N_ID]: n for n in self.nodes}
        self.members = {n[N_ID]: n[N_CAMP] for n in self.nodes if n[N_CAMP]}
        self.edge_key = {(e[E_SRC], e[E_TGT]): i for i, e in enumerate(self.edges)}
        self.adj: dict[str, list[int]] = defaultdict(list)
        for i, e in enumerate(self.edges):
            self.adj[e[E_SRC]].append(i)
            self.adj[e[E_TGT]].append(i)
        self.by_campaign: dict[str, list[str]] = defaultdict(list)
        for n in self.nodes:
            self.by_campaign[n[N_CAMP] or f"solo:{n[N_ID]}"].append(n[N_ID])

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> GraphBase:
        return cls(raw)

    def campaign_of(self, nid: str) -> str:
        return self.members.get(nid) or f"solo:{nid}"

    def overlay(self, conn: sqlite3.Connection, reviews: dict[tuple[str, str], str]
                ) -> tuple[list[list[Any]], dict[str, list[Any]]]:
        """Edges with review state (EDGE_COLUMNS order) and extra nodes that only analyst must-links bring in.
        Accepted pairs the scorer left independent become `review` edges (as in /explore)."""
        out = [e[:E_ROB + 1] + [REVIEW_OF.get(reviews.get((e[E_SRC], e[E_TGT])) or "")] + [e[E_REASONS]]
               for e in self.edges]
        extra_nodes: dict[str, list[Any]] = {}
        for (a, b), v in reviews.items():
            if v != "accepted" or (a, b) in self.edge_key:
                continue
            r = conn.execute("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs WHERE run_id=?"
                             " AND a=? AND b=?", (self.run_id, a, b)).fetchone()
            if r is None:
                continue
            e = edge_from_row(r[0], r[1], r[2], r[3], r[4], loads(r[5]))
            e[E_LEVEL] = e[E_LEVEL] or "review"
            out.append(e[:E_ROB + 1] + ["must_link"] + [e[E_REASONS]])
            for x in (a, b):
                if x not in self.node_by_id and x not in extra_nodes:
                    extra_nodes[x] = node_rows(conn, [x], self.members, self.campaigns)[0]
        return out, extra_nodes


def bounds_of(layout: dict[str, Any]) -> dict[str, float]:
    """Box around every campaign and group disc (as the overview `bounds`)."""
    discs = list(layout["campaigns"].values()) + list(layout["groups"].values())
    return {"x0": min((d[0] - d[2] for d in discs), default=0), "y0": min((d[1] - d[2] for d in discs), default=0),
            "x1": max((d[0] + d[2] for d in discs), default=0), "y1": max((d[1] + d[2] for d in discs), default=0)}


def degree_of(edges: list[list[Any]]) -> Counter:
    return Counter([e[E_SRC] for e in edges] + [e[E_TGT] for e in edges])


def node_out(n: list[Any], degree: Counter, layout: dict[str, Any]) -> list[Any]:
    xy = layout["nodes"].get(n[N_ID]) or [None, None]
    return [*n[:N_THUMB + 1], degree[n[N_ID]], xy[0], xy[1]]


def edge_out(e: list[Any], top: int) -> list[Any]:
    """Edge row in EDGE_COLUMNS order (review at 13) with `top` reasons."""
    return e[:14] + [e[14][:top]]


def table(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows}
