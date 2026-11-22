"""Variant lineage per campaign (GAPS_PAPERS G01): a time-directed spanning tree over the scored member pairs,
with what changed and what stayed on every edge.

Method
- Order. Members are ordered by `observed_at` (earliest date known from the material, parsed to UTC; ties broken
  by incident id). Members without a date follow the dated ones in import order (incidents rowid), then id. With no
  date at all the order is the import order and the tree is labelled `import_order`; with neither dates nor import
  ranks it is an undirected maximum spanning tree (`undirected`). The direction is the observation order, never a
  claim about the true edit order.
- Tree. Only pairs that were actually scored count. Each scored pair is oriented from the earlier to the later
  member, so the candidate graph is a DAG. On a DAG Edmonds' maximum spanning arborescence / maximum branching
  reduces to "every node keeps its strongest incoming edge" (a cycle is impossible), so each member takes as parent
  the earlier member with the highest campaign-level link strength (`support`, else `campaign_bits`, else
  `total_bits`; > 0 only), ties broken by the earlier position. Members without such a parent are roots (a forest
  when the earliest member does not reach every other one). `tests/core/test_lineage.py` checks the total weight
  against `networkx.maximum_branching`.
- Edge diff. Gene kinds are grouped into readable categories (domain, creative, text, contact, operator ids, kit,
  person, narrative, platform metadata). Kept = the pair's positive reasons (bits > 0) plus exact (or near-hash)
  value overlaps; changed = kind observed on both sides with disjoint values; added / removed = kind observed on
  one side only while the same extractor produced observations on the other side; otherwise the kind is unknown
  ("nieznane") on the other side, never a change (missing observation = unknown).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import networkx as nx

from hydra.entropy import is_stop

DIRECTION_PL = {
    "observation_order": "kolejność obserwacji (najwcześniejsza data z materiału)",
    "mixed": "kolejność obserwacji; członkowie bez daty dołączeni na końcu w kolejności importu",
    "import_order": "brak dat w materiale: kolejność importu, nie kolejność obserwacji",
    "undirected": "brak dat i kolejności importu: drzewo nieskierowane",
}

CATEGORY_ORDER = ["domain", "creative", "text", "contact", "operator", "kit", "person", "narrative", "meta", "other"]
CATEGORY_PL = {
    "domain": "domena", "creative": "kreacja", "text": "tekst", "contact": "kontakt",
    "operator": "identyfikatory operatora", "kit": "pliki zestawu", "person": "osoba / wizerunek",
    "narrative": "narracja", "meta": "metadane platformy", "other": "inne",
}
NEW_PL = {
    "domain": "nowa domena", "creative": "nowa kreacja", "text": "nowy tekst", "contact": "nowy kontakt",
    "operator": "nowe identyfikatory operatora", "kit": "nowe pliki zestawu", "person": "nowa osoba / wizerunek",
    "narrative": "nowa narracja", "meta": "nowe metadane platformy", "other": "inne zmiany",
}
HEADLINE_KINDS = {"ad_headline", "headline", "page_title", "title", "og_title", "library_ad_text", "ad_text"}
PIXEL_PREFIX = ("tracker", "pixel", "gtm", "fbq", "ga_")

_CONTACT = ("entity_phone", "entity_iban", "entity_email", "phone", "iban", "email", "account", "telegram",
            "whatsapp")
_OPERATOR = ("tracker", "pixel", "gtm", "fbq", "ga_", "operator_id", "platform_ad_id", "ad_id", "page_id",
             "kit_uuid", "landing_uuid", "uuid", "advertiser", "analytics")
_DOMAIN = ("registrable_domain", "regdom", "domain", "landing_host", "host", "redirect", "entry_url", "url",
           "ip", "asn", "as_org", "bgp", "ns_", "mx_", "dns_", "cert", "tls", "registrar", "registration")
_KIT = ("dom_", "js_", "css_", "tlsh", "asset_sha256", "favicon", "form_", "lead_form", "post_", "endpoint",
        "class_vocab", "data_attrs", "inline_", "lib", "sdk", "kit", "template", "cloak", "tds", "param",
        "skeleton", "comment", "script", "media_brand")
_KIT_TIERS = ("kit", "platform")

# Kinds that say nothing about a variant: classifiers, language, dates of capture or registration, boilerplate.
NOISE_KINDS = {"image_class", "language", "lang", "person_candidate", "brand_mention", "commenter_name",
               "domain_age_at_observation", "dns_rcode", "asr_status", "tls_invalid"}
NOISE_SUFFIXES = ("_month", "_day", "_date", "_week", "_time")
NOISE_TIERS = ("boilerplate", "label")
# Hashes whose exact inequality is not a change on its own: compared by Hamming distance where possible,
# reported as changed only when the category has no kept evidence on the edge.
NEAR_HASH = {"pdq": (256, 31), "phash_tile": (64, 6), "phash": (64, 10), "dhash": (64, 10), "ahash": (64, 6)}
FUZZY_PREFIX = ("artifact_sha256", "pdq", "phash", "dhash", "ahash", "minhash", "simhash", "tlsh", "dinov2",
                "siglip", "tvec", "e5", "embedding")
MAX_ITEMS = 40
MAX_VALUES = 3
VALUE_CHARS = 90


@dataclass(frozen=True)
class GeneRow:
    family: str
    kind: str
    value: str | None
    status: str = "observed"
    extractor: str = ""
    dependency_group: str | None = None


@dataclass
class PairLink:
    """One scored member pair as stored in `pairs.detail` (hydra/pipeline.py `_write_run`)."""

    a: str
    b: str
    support: float
    reasons: list[dict[str, Any]] = field(default_factory=list)
    hypothesis: str | None = None
    total_bits: float | None = None


# --------------------------------------------------------------------------------------- categories


def _tier(dep: str | None) -> str | None:
    parts = (dep or "").lower().split(":")
    return parts[1] if len(parts) >= 3 else None


def category(family: str, kind: str, dependency_group: str | None = None) -> str:
    """Readable category of a gene kind (heuristic name/family map, see module docstring)."""
    k, fam, tier = kind.lower(), str(family).upper(), _tier(dependency_group)
    if k.startswith(_CONTACT):
        return "contact"
    if k.startswith(_OPERATOR):
        return "operator"
    if fam == "INF" and (tier in _KIT_TIERS or k.startswith(_KIT)):
        return "kit"
    if fam == "INF" and k.startswith(_DOMAIN):
        return "domain"
    if fam == "INF" and tier == "operator":
        return "operator"
    if fam == "VIS":
        return "creative"
    if fam == "TXT":
        return "text"
    if fam == "ID":
        return "person"
    if fam == "NAR":
        return "narrative"
    if fam == "META":
        return "meta"
    if fam == "INF":
        return "kit" if tier in _KIT_TIERS else "domain"
    return "other"


def _is_noise(kind: str, dependency_group: str | None) -> bool:
    k = kind.lower()
    return k in NOISE_KINDS or k.endswith(NOISE_SUFFIXES) or _tier(dependency_group) in NOISE_TIERS


def _fuzzy(kind: str) -> bool:
    return kind.lower().startswith(FUZZY_PREFIX)


def _hex_bits(value: str) -> int | None:
    v = value.split(":")[-1].strip()
    try:
        return int(v, 16)
    except ValueError:
        return None


def _near(kind: str, va: set[str], vb: set[str]) -> bool:
    k = kind.lower()
    lim = next((t for p, (_, t) in sorted(NEAR_HASH.items(), key=lambda x: -len(x[0])) if k.startswith(p)), None)
    if lim is None:
        return False
    ha = [h for h in (_hex_bits(v) for v in va) if h is not None]
    hb = [h for h in (_hex_bits(v) for v in vb) if h is not None]
    return any(bin(x ^ y).count("1") <= lim for x in ha for y in hb)


# --------------------------------------------------------------------------------------- order


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        try:
            d = datetime.fromisoformat(t[:10])
        except ValueError:
            return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def observation_order(members: Iterable[str], observed_at: Mapping[str, str | None],
                      import_rank: Mapping[str, int] | None = None) -> tuple[list[str], str]:
    """Members in observation order and the direction label (see module docstring)."""
    ids = sorted(set(members))
    rank = import_rank or {}
    times = {i: _parse_time(observed_at.get(i)) for i in ids}
    dated = [i for i in ids if times[i] is not None]
    undated = [i for i in ids if times[i] is None]
    by_import = sorted(undated, key=lambda i: (rank.get(i, 1 << 62), i))
    order = sorted(dated, key=lambda i: (times[i], i)) + by_import
    if len(dated) == len(ids):
        return order, "observation_order"
    if dated:
        return order, "mixed"
    if rank and all(i in rank for i in ids):
        return order, "import_order"
    return ids, "undirected"


# --------------------------------------------------------------------------------------- diff


def _values(rows: Iterable[GeneRow]) -> tuple[dict[str, set[str]], dict[str, GeneRow], set[str], set[str]]:
    """kind -> observed values, kind -> a representative row, kinds marked unavailable, extractors that observed."""
    vals: dict[str, set[str]] = defaultdict(set)
    rep: dict[str, GeneRow] = {}
    unknown: set[str] = set()
    extractors: set[str] = set()
    for g in rows:
        if _is_noise(g.kind, g.dependency_group):
            continue
        if g.status != "observed":
            unknown.add(g.kind)
            continue
        rep.setdefault(g.kind, g)
        extractors.add(g.extractor.split("@")[0])
        if g.value is not None and not is_stop(g.kind, g.value):
            vals[g.kind].add(g.value)
        else:
            vals.setdefault(g.kind, set())
    return dict(vals), rep, unknown, extractors


def _short(values: Iterable[str]) -> list[str]:
    out = sorted(values)[:MAX_VALUES]
    return [v if len(v) <= VALUE_CHARS else v[: VALUE_CHARS - 1] + "…" for v in out]


def _item(kind: str, row: GeneRow | dict[str, Any], **extra: Any) -> dict[str, Any]:
    if isinstance(row, GeneRow):
        fam, dep = row.family, row.dependency_group
    else:
        fam, dep = str(row.get("family") or ""), None
    cat = category(fam, kind, dep)
    return {"kind": kind, "family": fam, "category": cat, "category_pl": CATEGORY_PL[cat], **extra}


def _sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: (-(x.get("bits") or 0.0), CATEGORY_ORDER.index(x["category"]), x["kind"]))


def edge_diff(parent: list[GeneRow], child: list[GeneRow], reasons: list[dict[str, Any]]) -> dict[str, Any]:
    """Kept / changed / added / removed / unknown gene kinds between an earlier (parent) and later (child) member."""
    pv, prep, punk, pext = _values(parent)
    cv, crep, cunk, cext = _values(child)
    kept: dict[str, dict[str, Any]] = {}
    for r in reasons:
        bits = float(r.get("bits") or 0.0)
        kind = str(r.get("kind") or "")
        if bits <= 0 or not kind:
            continue
        rep = prep.get(kind) or crep.get(kind) or r
        if kind in kept:
            kept[kind]["bits"] = round(kept[kind]["bits"] + bits, 3)
            continue
        shown = {v for v in (r.get("value_a"), r.get("value_b")) if isinstance(v, str)}
        kept[kind] = _item(kind, rep, bits=round(bits, 3), values=_short(shown), explanation_pl=r.get("explanation_pl"))
    changed, added, removed, unknown = [], [], [], []
    for kind in sorted(set(pv) | set(cv) | punk | cunk):
        a, b = pv.get(kind), cv.get(kind)
        rep = prep.get(kind) or crep.get(kind)
        if a is not None and b is not None:
            if kind in kept or not a or not b:
                continue
            shared = a & b
            if shared or _near(kind, a, b):
                kept[kind] = _item(kind, rep, bits=None, values=_short(shared or a))
            else:
                changed.append(_item(kind, rep, before=_short(a), after=_short(b), fuzzy=_fuzzy(kind)))
        elif b and rep is not None:
            if kind in kept:
                continue
            if kind not in punk and rep.extractor.split("@")[0] in pext:
                added.append(_item(kind, rep, after=_short(b)))
            else:
                unknown.append(_item(kind, rep, side="parent"))
        elif a and rep is not None:
            if kind in kept:
                continue
            if kind not in cunk and rep.extractor.split("@")[0] in cext:
                removed.append(_item(kind, rep, before=_short(a)))
            else:
                unknown.append(_item(kind, rep, side="child"))
    kept_cats = {k["category"] for k in kept.values()}
    # a different file hash / perceptual hash is not a new creative when the same category kept evidence
    changed = [c for c in changed if not (c.pop("fuzzy") and c["category"] in kept_cats)]
    return {"kept": _sort(list(kept.values())), "changed": _sort(changed), "added": _sort(added),
            "removed": _sort(removed), "unknown": _sort(unknown)}


def _cats(items: list[dict[str, Any]]) -> list[str]:
    return sorted({i["category"] for i in items}, key=CATEGORY_ORDER.index)


def summary_pl(child: str, parent: str, diff: dict[str, Any]) -> str:
    changed = _cats(diff["changed"])
    kept_strong = [i for i in diff["kept"] if (i.get("bits") or 0) > 0]
    kept = _cats(diff["kept"])
    parts: list[str] = []
    for cat in changed:
        if cat == "text" and any(i["kind"] in HEADLINE_KINDS for i in diff["changed"] if i["category"] == cat):
            parts.append("nowy nagłówek")
        else:
            parts.append(NEW_PL[cat])
    added = [c for c in _cats(diff["added"]) if c not in changed]
    removed = [c for c in _cats(diff["removed"]) if c not in changed]
    if added:
        parts.append("doszło: " + ", ".join(CATEGORY_PL[c] for c in added))
    if removed:
        parts.append("ubyło: " + ", ".join(CATEGORY_PL[c] for c in removed))
    head = f"wariant {child} (po {parent}): " + (", ".join(parts) if parts else "bez zmian w porównywalnych genach")

    def kept_label(cat: str) -> str:
        if cat == "operator" and any(i["kind"].lower().startswith(PIXEL_PREFIX)
                                     for i in diff["kept"] if i["category"] == cat):
            return "piksel"
        return CATEGORY_PL[cat]

    full = [kept_label(c) for c in kept if c not in changed]
    partial = [kept_label(c) for c in kept if c in changed]
    tail = ""
    if full:
        tail += "; bez zmian: " + ", ".join(full)
    if partial:
        tail += "; częściowo bez zmian: " + ", ".join(partial)
    if not kept_strong:
        tail += "; brak wspólnego dowodu z bitami > 0"
    return head + tail


# --------------------------------------------------------------------------------------- tree


def build_lineage(members: Iterable[str], observed_at: Mapping[str, str | None], pairs: Iterable[PairLink],
                  genes: Mapping[str, list[GeneRow]], import_rank: Mapping[str, int] | None = None
                  ) -> dict[str, Any]:
    """Lineage of one campaign: {direction, direction_pl, nodes, edges}. Deterministic for the same input."""
    order, direction = observation_order(members, observed_at, import_rank)
    pos = {iid: i for i, iid in enumerate(order)}
    links: dict[tuple[str, str], PairLink] = {}
    for p in pairs:
        if p.a in pos and p.b in pos and p.a != p.b and p.support > 0:
            key = tuple(sorted((p.a, p.b)))
            if key not in links or p.support > links[key].support:
                links[key] = p  # type: ignore[index]
    parent: dict[str, str] = {}
    if direction == "undirected":
        g = nx.Graph()
        g.add_nodes_from(order)
        for (a, b), p in sorted(links.items()):
            g.add_edge(a, b, weight=p.support)
        tree = nx.maximum_spanning_tree(g, weight="weight")
        for comp in sorted((sorted(c) for c in nx.connected_components(tree)), key=lambda c: c[0]):
            for u, v in nx.bfs_edges(tree, comp[0], sort_neighbors=sorted):
                parent[v] = u
    else:
        best: dict[str, tuple[float, int, str]] = {}
        for (a, b), p in links.items():
            src, dst = (a, b) if pos[a] < pos[b] else (b, a)
            cand = (p.support, -pos[src], src)
            if dst not in best or cand > best[dst]:
                best[dst] = cand
        parent = {dst: c[2] for dst, c in best.items()}
    depth: dict[str, int] = {}

    def d(i: str) -> int:
        if i not in depth:
            depth[i] = 0 if i not in parent else d(parent[i]) + 1
        return depth[i]

    nodes = [{"incident_id": iid, "observed_at": observed_at.get(iid), "order": pos[iid] + 1,
              "root": iid not in parent, "parent": parent.get(iid), "depth": d(iid)} for iid in order]
    edges = []
    for child in sorted(parent, key=lambda c: pos[c]):
        par = parent[child]
        link = links[tuple(sorted((par, child)))]  # type: ignore[index]
        diff = edge_diff(genes.get(par, []), genes.get(child, []), link.reasons)
        summary = summary_pl(child, par, diff)  # from the full diff, before the lists are capped
        counts = {k: len(v) for k, v in diff.items()}
        edges.append({"parent": par, "child": child, "bits": round(link.support, 3),
                      "hypothesis": link.hypothesis, "total_bits": link.total_bits,
                      **{k: v[:MAX_ITEMS] for k, v in diff.items()}, "counts": counts, "summary_pl": summary})
    return {"direction": direction, "direction_pl": DIRECTION_PL[direction], "nodes": nodes, "edges": edges,
            "roots": [n["incident_id"] for n in nodes if n["root"]]}


NOTE_PL = ("Linia wariantów: każdy członek jest podłączony do wcześniejszego członka, z którym ma najsilniejsze "
           "ocenione powiązanie (bity poziomu kampanii). Kierunek to kolejność obserwacji, nie udowodniona "
           "kolejność edycji. Bity to wagi dowodu, nie prawdopodobieństwa. Brak obserwacji po jednej stronie to "
           "„nieznane”, nie zmiana.")


# --------------------------------------------------------------------------------------- storage


def _link_strength(detail: dict[str, Any], total_bits: float | None) -> float:
    for key in ("support", "campaign_bits"):
        v = detail.get(key)
        if isinstance(v, int | float):
            return float(v)
    return float(total_bits or 0.0)


def lineage_for_campaign(conn: sqlite3.Connection, run_id: str, campaign_id: str) -> dict[str, Any] | None:
    """Lineage of a stored campaign, computed on demand from the run's tables (old runs need no re-analysis).
    None when the campaign is not in the run."""
    import json

    row = conn.execute("SELECT detail FROM campaigns WHERE run_id=? AND id=?", (run_id, campaign_id)).fetchone()
    if row is None:
        return None
    members = [r[0] for r in conn.execute(
        "SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=? ORDER BY incident_id",
        (run_id, campaign_id))]
    if not members:
        try:
            members = list((json.loads(row[0] or "{}") or {}).get("members") or [])
        except ValueError:
            members = []
    marks = ",".join("?" * len(members)) or "NULL"
    observed, rank = {}, {}
    for r in conn.execute(f"SELECT id, observed_at, rowid FROM incidents WHERE id IN ({marks})", members):
        observed[r[0]] = r[1]
        rank[r[0]] = int(r[2])
    pairs = []
    if len(members) > 1:
        for r in conn.execute(f"SELECT a, b, total_bits, hypothesis, detail FROM pairs WHERE run_id=? AND a IN"
                              f" ({marks}) AND b IN ({marks})", [run_id, *members, *members]):
            try:
                detail = json.loads(r[4] or "{}") or {}
            except ValueError:
                detail = {}
            pairs.append(PairLink(a=r[0], b=r[1], support=_link_strength(detail, r[2]),
                                  reasons=list(detail.get("reasons") or []), hypothesis=r[3], total_bits=r[2]))
    genes: dict[str, list[GeneRow]] = defaultdict(list)
    if len(members) > 1:
        for r in conn.execute(f"SELECT incident_id, family, kind, value, status, extractor, dependency_group FROM"
                              f" genes WHERE incident_id IN ({marks}) ORDER BY incident_id, id", members):
            genes[r[0]].append(GeneRow(family=r[1], kind=r[2], value=r[3], status=r[4], extractor=r[5] or "",
                                       dependency_group=r[6]))
    out = build_lineage(members, observed, pairs, genes, rank)
    from hydra import lineage_mutation as lm  # ADV-CAMP: spec M7 tree by mutation distance (HYDRA_ADV_LINEAGE=1)

    if lm.enabled():
        out["mutation_tree"] = lm.mutation_tree(members, observed, pairs, genes, rank,
                                                {x["incident_id"]: x["parent"] for x in out["nodes"]})
    return {"campaign_id": campaign_id, "run_id": run_id, **out, "note_pl": NOTE_PL}
