"""Neo4j export of one analyse run, no Neo4j dependency. One graph builder, three renderings.

`build_graph(conn, run_id)` returns the node and relationship rows; every output is rendered from it, so the CLI
(`hydra export-neo4j`) and the API (`GET /v1/export/neo4j`) produce the same graph:

1. CSV files + `import.cypher` (`LOAD CSV`): `export_run` writes the CLI folder; API `format=zip` returns it zipped.
   Copy the folder into Neo4j's `import/` and run `cypher-shell -f import.cypher`.
2. `graph.cypher`, self-contained (`UNWIND` of inline rows + `MERGE`): `inline_cypher`; also in the CLI folder;
   API `format=cypher`. Run `cypher-shell -f graph.cypher` or paste it into the Browser.
3. `neo4j-admin database import` node / relationship CSV: `admin_csv`; API `format=nodes_csv` / `edges_csv`.

Nodes: `Incident` (campaign members and link endpoints), `Campaign`, `CampaignGroup`, `Gene` (one node per
(kind, value); by default only values shared by >= 2 exported incidents, i.e. pivots; stop-genes and embeddings
excluded). Relationships: `(Incident)-[:LINKED_TO {probability, bits, hypothesis, calibrated, model, top_reason}]->
(Incident)`, `(Incident)-[:HAS_GENE {family, provenance}]->(Gene)`, `(Incident)-[:MEMBER_OF]->(Campaign)`,
`(Campaign)-[:IN_GROUP]->(CampaignGroup)`. Every string value passes `hydra.redact.redact_text`; a gene value changed
by redaction gets a salted fingerprint as its node key, so two different secrets never merge into one node.

Serialisation safety (CR-ASTRA-SEC-10/11/13/16, merged from integration): every CSV cell of every rendering passes
`hydra.export_safe.csv_cell` (redaction, control characters, spreadsheet formulas neutralised with a leading
apostrophe); every Cypher string literal is `hydra.export_safe.cypher_string` (double-quoted, quotes, backslashes
and all control characters escaped) and the run id in comments is `cypher_comment` (no line terminator). Gene keys
are `<kind>|sha256(kind|value)[:32]` (`gene_key`): collision-resistant identities from the complete value that never
expose the value itself; values changed by redaction use the salted fingerprint instead.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hydra.contracts import Gene as GeneModel
from hydra.entropy import is_stop
from hydra.export_safe import csv_cell, cypher_comment, cypher_string
from hydra.redact import redact_text, secret_fingerprint
from hydra.synapse import gene_tier, level

# Property types: "str", "int", "float", "bool". Order = CSV column order after the key column(s).
NODES: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    # file stem, label, key property, other properties
    ("incidents", "Incident", "id", (("title", "str"), ("observed_at", "str"), ("source", "str"))),
    ("campaigns", "Campaign", "id", (("label", "str"), ("size", "int"), ("confidence", "float"), ("scheme", "str"),
                                     ("run_id", "str"))),
    ("groups", "CampaignGroup", "id", (("label", "str"), ("confidence", "float"))),
    ("genes", "Gene", "key", (("kind", "str"), ("value", "str"), ("family", "str"), ("doc_freq", "int"),
                              ("tier", "str"), ("level", "str"))),
)
RELS: tuple[tuple[str, str, tuple[str, str, str], tuple[str, str, str], tuple[tuple[str, str, str], ...]], ...] = (
    # file stem, type, (start label, CSV column, key), (end label, CSV column, key), (property, CSV column, type)
    ("member_of", "MEMBER_OF", ("Incident", "incident_id", "id"), ("Campaign", "campaign_id", "id"), ()),
    ("in_group", "IN_GROUP", ("Campaign", "campaign_id", "id"), ("CampaignGroup", "group_id", "id"), ()),
    ("has_gene", "HAS_GENE", ("Incident", "incident_id", "id"), ("Gene", "gene_key", "key"),
     (("family", "family", "str"), ("provenance", "provenance", "str"))),
    ("linked_to", "LINKED_TO", ("Incident", "a", "id"), ("Incident", "b", "id"),
     (("probability", "probability", "float"), ("bits", "total_bits", "float"), ("hypothesis", "hypothesis", "str"),
      ("calibrated", "calibrated", "bool"), ("model", "model", "str"), ("top_reason", "top_reason", "str"))),
)
CONSTRAINTS = (("incident_id", "Incident", "id"), ("campaign_id", "Campaign", "id"),
               ("group_id", "CampaignGroup", "id"), ("gene_key", "Gene", "key"))
BATCH = 500  # rows per UNWIND statement in the self-contained script


@dataclass
class Graph:
    run_id: str
    tables: dict[str, tuple[list[str], list[list[Any]]]] = field(default_factory=dict)  # stem -> (header, rows)

    def counts(self) -> dict[str, int]:
        return {stem: len(rows) for stem, (_h, rows) in self.tables.items()}


def _red(v: Any) -> Any:
    return redact_text(v) if isinstance(v, str) else v


def gene_key(kind: str, value: str) -> str:
    """Stable graph identity of a gene node from its complete typed value; the value itself is not exposed."""
    return f"{kind}|" + hashlib.sha256(f"{kind}|{value}".encode()).hexdigest()[:32]


def _gene_key(kind: str, value: str) -> tuple[str, str]:
    """(node key, shown value): the key never carries the value (CR-ASTRA-SEC-16: no truncation collisions)."""
    shown = redact_text(value) or ""
    if shown != value:
        return f"{kind}|{secret_fingerprint(value)}", shown[:400]
    return gene_key(kind, value), shown[:400]


def _tier_level(family: str, kind: str, value: str, extractor: str, dep: str | None) -> tuple[str, str]:
    """Evidence tier (`synapse.gene_tier`: operator, platform, kit, ...; "" when the gene has none) and the level a
    match counts at (`synapse.level`: campaign | kit | asset | none), so Cypher can filter on HYDRA's own rules."""
    g = GeneModel(incident_id="x", family=family, kind=kind, value=value, extractor=extractor or "export@1",
                  dependency_group=dep)
    return gene_tier(g) or "", level(g)


def build_graph(conn: sqlite3.Connection, run_id: str, *, min_df: int = 2, min_bits: float = float("-inf"),
                min_p: float = 0.0) -> Graph:
    """Node and relationship rows of `run_id` (typed Python values, already redacted)."""
    camp_rows = conn.execute("SELECT id, label, confidence, detail FROM campaigns WHERE run_id=? ORDER BY id",
                             (run_id,)).fetchall()
    members: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id=? ORDER BY 1, 2",
                          (run_id,)):
        members[r["campaign_id"]].append(r["incident_id"])
    campaigns = []
    for r in camp_rows:
        d = json.loads(r["detail"] or "{}")
        mem = members.get(r["id"]) or sorted(d.get("members") or [])
        campaigns.append({"id": r["id"], "label": r["label"] or d.get("label") or "", "members": mem,
                          "confidence": r["confidence"] if r["confidence"] is not None else d.get("confidence"),
                          "scheme": d.get("scheme") or "", "group_id": d.get("group_id")})
    groups = [json.loads(r["detail"] or "{}") | {"id": r["id"], "label": r["label"], "confidence": r["confidence"]}
              for r in conn.execute("SELECT id, label, confidence, detail FROM campaign_groups WHERE run_id=?"
                                    " ORDER BY id", (run_id,))]
    if not groups:  # CR-CORE-1: older runs keep groups in runs.metrics
        run = conn.execute("SELECT metrics FROM runs WHERE id=?", (run_id,)).fetchone()
        groups = [g for g in (json.loads(run["metrics"] or "{}").get("campaign_groups") or [] if run else [])
                  if isinstance(g, dict) and g.get("id")]
    camp_group = {c["id"]: c["group_id"] for c in campaigns if c.get("group_id")}
    for g in groups:
        for cid in g.get("campaign_ids") or []:
            camp_group.setdefault(cid, g["id"])
    known_groups = {g["id"] for g in groups}

    link_rows = []
    for r in conn.execute("SELECT a, b, total_bits, probability, calibrated, hypothesis, model, detail FROM pairs"
                          " WHERE run_id=? AND total_bits >= ? AND COALESCE(probability, 0) >= ? ORDER BY a, b",
                          (run_id, min_bits, min_p)):
        reasons = json.loads(r["detail"] or "{}").get("reasons") or []
        top = reasons[0].get("explanation_pl") or "" if reasons else ""
        link_rows.append([r["a"], r["b"], r["probability"], r["total_bits"], r["hypothesis"], bool(r["calibrated"]),
                          r["model"], _red(top)])
    inc_ids = sorted({m for c in campaigns for m in c["members"]} | {x for r in link_rows for x in r[:2]})
    qmarks = ",".join("?" * len(inc_ids)) or "''"
    g = Graph(run_id)
    g.tables["incidents"] = (["id", "title", "observed_at", "source"], [
        [r["id"], _red(r["title"] or ""), r["observed_at"] or "", _red(r["source"])]
        for r in conn.execute(f"SELECT id, title, observed_at, source FROM incidents WHERE id IN ({qmarks})"
                              " ORDER BY id", inc_ids)])
    g.tables["campaigns"] = (["id", "label", "size", "confidence", "scheme", "run_id"], [
        [c["id"], _red(c["label"]), len(c["members"]), c["confidence"], _red(c["scheme"]), run_id] for c in campaigns])
    g.tables["groups"] = (["id", "label", "confidence"], [
        [x["id"], _red(x.get("label") or ""), x.get("confidence")] for x in groups])
    g.tables["member_of"] = (["incident_id", "campaign_id"], [[m, c["id"]] for c in campaigns for m in c["members"]])
    g.tables["in_group"] = (["campaign_id", "group_id"], [
        [c["id"], camp_group[c["id"]]] for c in campaigns if camp_group.get(c["id"]) in known_groups])

    holders: dict[tuple[str, str], set[str]] = defaultdict(set)
    fam: dict[tuple[str, str], tuple[str, str]] = {}
    for r in conn.execute(
        f"SELECT incident_id, family, kind, value, provenance, extractor, dependency_group FROM genes"
        f" WHERE incident_id IN ({qmarks})"
        " AND status='observed' AND value IS NOT NULL AND vector_ref IS NULL ORDER BY id", inc_ids):
        key = (r["kind"], r["value"])
        if is_stop(*key):
            continue
        holders[key].add(r["incident_id"])
        fam.setdefault(key, (r["family"], r["provenance"], r["extractor"], r["dependency_group"]))
    gene_rows, has_rows = [], []
    for (kind, value), inc in sorted(holders.items()):
        if len(inc) < min_df:
            continue
        gkey, shown = _gene_key(kind, value)
        family, prov, extractor, dep = fam[(kind, value)]
        tier, lvl = _tier_level(family, kind, value, extractor, dep)
        gene_rows.append([gkey, kind, shown, family, len(inc), tier, lvl])
        has_rows.extend([i, gkey, family, prov] for i in sorted(inc))
    g.tables["genes"] = (["key", "kind", "value", "family", "doc_freq", "tier", "level"], gene_rows)
    g.tables["has_gene"] = (["incident_id", "gene_key", "family", "provenance"], has_rows)
    g.tables["linked_to"] = (["a", "b", "probability", "total_bits", "hypothesis", "calibrated", "model",
                              "top_reason"], link_rows)
    return g


def subgraph(graph: Graph, keep: set[str]) -> Graph:
    """The part of `graph` around the selected incidents (UI graph selection, `POST /v1/graph/export`): those
    incidents, their campaigns and groups, links with both ends selected, and their gene values. Same labels,
    relationship types and properties as the full export, so the documented queries run on it."""
    t = graph.tables
    out = Graph(graph.run_id)
    inc_h, inc = t["incidents"]
    out.tables["incidents"] = (inc_h, [r for r in inc if r[0] in keep])
    mem_h, mem = t["member_of"]
    mem = [r for r in mem if r[0] in keep]
    out.tables["member_of"] = (mem_h, mem)
    cids = {r[1] for r in mem}
    camp_h, camp = t["campaigns"]
    out.tables["campaigns"] = (camp_h, [r for r in camp if r[0] in cids])
    ig_h, ig = t["in_group"]
    ig = [r for r in ig if r[0] in cids]
    out.tables["in_group"] = (ig_h, ig)
    gids = {r[1] for r in ig}
    grp_h, grp = t["groups"]
    out.tables["groups"] = (grp_h, [r for r in grp if r[0] in gids])
    hg_h, hg = t["has_gene"]
    hg = [r for r in hg if r[0] in keep]
    out.tables["has_gene"] = (hg_h, hg)
    gkeys = {r[1] for r in hg}
    gen_h, gen = t["genes"]
    out.tables["genes"] = (gen_h, [r for r in gen if r[0] in gkeys])
    lk_h, lk = t["linked_to"]
    out.tables["linked_to"] = (lk_h, [r for r in lk if r[0] in keep and r[1] in keep])
    return out


# --------------------------------------------------------------------------------------- renderings


def _csv_cell(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and not math.isfinite(v):
        return ""
    return csv_cell(v)  # redaction, controls, formula prefixes (CR-ASTRA-SEC-10)


def _csv_text(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows([[_csv_cell(v) for v in row] for row in rows])
    return buf.getvalue()


# `export_safe.csv_cell` prefixes a text starting with a formula character with an apostrophe (spreadsheet safety);
# LOAD CSV removes exactly that apostrophe again, so a phone "+48..." is the same value as in `graph.cypher`.
_UNFORMULA = '"(?s)\'( *[-=+@\uff1d\uff0b\uff0d\uff20]|[\\t\\r]).*"'


def _cast(expr: str, typ: str) -> str:
    if typ == "str":
        return f"CASE WHEN {expr} =~ {_UNFORMULA} THEN substring({expr}, 1) ELSE {expr} END"
    return {"int": f"toInteger({expr})", "float": f"toFloat({expr})", "bool": f"{expr} = 'true'"}.get(typ, expr)


def _header(run_id: str, how: str) -> list[str]:
    head = f"// HYDRA NEXUS run {cypher_comment(run_id)}: generated by hydra/export_neo4j.py ({cypher_comment(how)})"
    return [head] + [
        f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE;"
        for name, label, key in CONSTRAINTS]


def load_csv_cypher(run_id: str) -> str:
    """`import.cypher`: LOAD CSV of the files written by `export_run` (Neo4j `import/` folder)."""
    lines = _header(run_id, "LOAD CSV")
    for stem, label, key, props in NODES:
        sets = ", ".join(f"n.{p} = {_cast('r.' + p, t)}" for p, t in props)
        lines += [f"LOAD CSV WITH HEADERS FROM 'file:///{stem}.csv' AS r",
                  f"  MERGE (n:{label} {{{key}: r.{key}}}) SET {sets};"]
    for stem, typ, (sl, scol, skey), (el, ecol, ekey), props in RELS:
        sets = (" SET " + ", ".join(f"x.{p} = {_cast('r.' + c, t)}" for p, c, t in props)) if props else ""
        lines += [f"LOAD CSV WITH HEADERS FROM 'file:///{stem}.csv' AS r",
                  f"  MATCH (s:{sl} {{{skey}: r.{scol}}}), (e:{el} {{{ekey}: r.{ecol}}})",
                  f"  MERGE (s)-[x:{typ}]->(e){sets};"]
    return "\n".join(lines) + "\n"


def cypher_value(v: Any) -> str:
    """Cypher literal of a Python scalar (strings via `export_safe.cypher_string`: double-quoted, every quote,
    backslash and control character escaped; NaN/inf -> null)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v) if math.isfinite(v) else "null"
    return cypher_string(str(v))


def _typed(v: Any, typ: str) -> Any:
    if v in (None, ""):
        return None
    if typ == "int":
        return int(v)
    if typ == "float":
        return float(v)
    if typ == "bool":
        return v if isinstance(v, bool) else str(v).lower() == "true"
    return str(v)


def _map(cols: list[str], types: list[str], row: list[Any]) -> str:
    return "{" + ", ".join(f"{c}: {cypher_value(_typed(v, t))}" for c, t, v in zip(cols, types, row, strict=True)) + "}"


def _unwind(rows: list[list[Any]], cols: list[str], types: list[str], tail: str) -> list[str]:
    out = []
    for i in range(0, len(rows), BATCH):
        body = ",\n".join("  " + _map(cols, types, row) for row in rows[i:i + BATCH])
        out.append(f"UNWIND [\n{body}\n] AS r\n{tail};")
    return out


def inline_cypher(graph: Graph) -> str:
    """Self-contained script (`graph.cypher`, API `format=cypher`): the same labels, types and properties as
    `import.cypher`, with the rows inline (`UNWIND [...] AS r MERGE ...`)."""
    counts = graph.counts()
    lines = _header(graph.run_id, "self-contained, " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for stem, label, key, props in NODES:
        header, rows = graph.tables[stem]
        types = [dict(props).get(c, "str") for c in header]
        sets = ", ".join(f"n.{p} = r.{p}" for p, _t in props)
        lines += _unwind(rows, header, types, f"MERGE (n:{label} {{{key}: r.{key}}}) SET {sets}")
    for stem, typ, (sl, scol, skey), (el, ecol, ekey), props in RELS:
        header, rows = graph.tables[stem]
        ptypes = {c: t for _p, c, t in props}
        types = [ptypes.get(c, "str") for c in header]
        sets = (" SET " + ", ".join(f"x.{p} = r.{c}" for p, c, _t in props)) if props else ""
        lines += _unwind(rows, header, types, f"MATCH (s:{sl} {{{skey}: r.{scol}}}), (e:{el} {{{ekey}: r.{ecol}}})\n"
                                              f"MERGE (s)-[x:{typ}]->(e){sets}")
    return "\n".join(lines) + "\n"


_ADMIN_TYPE = {"int": "int", "float": "float", "bool": "boolean"}


def admin_csv(graph: Graph, which: str) -> str:
    """`neo4j-admin database import` CSV: `nodes` (all labels in one file, internal `:ID` = `<Label>:<key>`, the
    key itself stays a property) or `edges` (all relationship types)."""
    if which == "nodes":
        cols: list[tuple[str, str]] = []
        for _s, _l, key, props in NODES:
            for p, t in ((key, "str"), *props):
                if all(p != c for c, _t in cols):
                    cols.append((p, t))
        header = [":ID", ":LABEL"] + [p + (f":{_ADMIN_TYPE[t]}" if t in _ADMIN_TYPE else "") for p, t in cols]
        rows = []
        for stem, label, key, _props in NODES:
            h, data = graph.tables[stem]
            for row in data:
                d = dict(zip(h, row, strict=True))
                rows.append([f"{label}:{d[key]}", label, *[d.get(p) for p, _t in cols]])
        return _csv_text(header, rows)
    pcols: list[tuple[str, str, str]] = []
    for *_x, props in RELS:
        for p, c, t in props:
            if all(p != q for q, _c, _t in pcols):
                pcols.append((p, c, t))
    header = [":START_ID", ":END_ID", ":TYPE"] + [p + (f":{_ADMIN_TYPE[t]}" if t in _ADMIN_TYPE else "")
                                                   for p, _c, t in pcols]
    rows = []
    for stem, typ, (sl, scol, _sk), (el, ecol, _ek), props in RELS:
        h, data = graph.tables[stem]
        mine = {p: c for p, c, _t in props}
        for row in data:
            d = dict(zip(h, row, strict=True))
            rows.append([f"{sl}:{d[scol]}", f"{el}:{d[ecol]}", typ,
                         *[d.get(mine[p]) if p in mine else None for p, _c, _t in pcols]])
    return _csv_text(header, rows)


def write_folder(graph: Graph, out_dir: Path) -> dict[str, int]:
    """CSV per node/relationship table + `import.cypher` (LOAD CSV) + `graph.cypher` (self-contained)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem, (header, rows) in graph.tables.items():
        (out_dir / f"{stem}.csv").write_text(_csv_text(header, rows), encoding="utf-8")
    (out_dir / "import.cypher").write_text(load_csv_cypher(graph.run_id), encoding="utf-8")
    (out_dir / "graph.cypher").write_text(inline_cypher(graph), encoding="utf-8")
    return graph.counts()


def export_run(conn: sqlite3.Connection, run_id: str, out_dir: Path, *, min_df: int = 2,
               min_bits: float = float("-inf"), min_p: float = 0.0) -> dict[str, int]:
    """CLI `hydra export-neo4j`: write the folder for `run_id`; returns row counts per table."""
    return write_folder(build_graph(conn, run_id, min_df=min_df, min_bits=min_bits, min_p=min_p), out_dir)
