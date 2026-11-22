"""B19: Neo4j API export = CLI export (same graph), the documented NEO4J.md queries only reference what both
produce, the self-contained Cypher script is syntactically sane, and the enriched dataset ZIP via the API equals
the CLI folder (with the assignments table). Synthetic corpus only; no Neo4j server."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings, get_settings
from hydra.db import connect
from hydra.export_dataset import export_dataset
from hydra.export_neo4j import NODES, RELS, export_run
from hydra.pipeline import analyse
from tests.core.test_pipeline import N_INC, corpus, test_extractors  # noqa: F401 — fixture

DOC = Path(__file__).resolve().parents[2] / "docs" / "NEO4J.md"


@pytest.fixture
def run(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    get_settings.cache_clear()
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        counts = export_run(conn, res.run_id, tmp_path / "cli")
    with TestClient(create_app(settings)) as client:
        yield res.run_id, client, tmp_path / "cli", counts
    get_settings.cache_clear()


# ------------------------------------------------------------------ minimal Cypher reader (test helper only)

# strings: export_safe.cypher_string literals are double-quoted with JSON escapes (merge-batch3); single quotes
# kept for hand-written Cypher
_TOKEN = re.compile(r"\s*(?:(?P<str>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")|(?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
                    r"|(?P<word>[A-Za-z_]\w*)|(?P<sym>[\[\]{}:,]))")
_ESC = {"n": "\n", "r": "\r", "t": "\t", "'": "'", "\\": "\\"}


def _literal(text: str):
    """Parse a Cypher list/map literal of scalars (the UNWIND payload) into Python."""
    toks = []
    pos = 0
    while pos < len(text.rstrip()):
        m = _TOKEN.match(text, pos)
        assert m, f"bad literal at {text[pos:pos + 40]!r}"
        toks.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    it = iter(toks)

    def val(tok):
        kind, v = tok
        if kind == "str" and v.startswith('"'):
            return json.loads(v)
        if kind == "str":
            return re.sub(r"\\(.)", lambda e: _ESC[e.group(1)], v[1:-1])
        if kind == "num":
            return float(v) if any(c in v for c in ".eE") else int(v)
        if kind == "word":
            return {"null": None, "true": True, "false": False}[v]
        if v == "[":
            out, t = [], next(it)
            while t != ("sym", "]"):
                out.append(val(t))
                t = next(it)
                if t == ("sym", ","):
                    t = next(it)
            return out
        if v == "{":
            out, t = {}, next(it)
            while t != ("sym", "}"):
                key = t[1]
                assert next(it) == ("sym", ":")
                out[key] = val(next(it))
                t = next(it)
                if t == ("sym", ","):
                    t = next(it)
            return out
        raise AssertionError(tok)

    return val(next(it))


def _statements(script: str) -> list[str]:
    """Split on top-level `;` outside strings; asserts balanced brackets and closed quotes."""
    out, cur, depth, quote, esc = [], [], [], None, False
    pairs = {")": "(", "]": "[", "}": "{"}
    for line in script.splitlines(keepends=True):
        if quote is None and not depth and line.lstrip().startswith("//"):
            continue
        for ch in line:
            cur.append(ch)
            if quote:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == quote:
                    quote = None
                continue
            if ch in "'\"`":
                quote = ch
            elif ch in "([{":
                depth.append(ch)
            elif ch in ")]}":
                assert depth and depth.pop() == pairs[ch], "unbalanced brackets"
            elif ch == ";" and not depth:
                out.append("".join(cur).strip())
                cur = []
    assert quote is None and not depth, "unclosed quote or bracket"
    assert not "".join(cur).strip(), f"statement without ';': {''.join(cur)[:80]!r}"
    return out


KNOWN_CLAUSES = {"CREATE", "CONSTRAINT", "IF", "NOT", "EXISTS", "FOR", "REQUIRE", "IS", "UNIQUE", "UNWIND", "AS",
                 "MERGE", "MATCH", "SET", "LOAD", "CSV", "WITH", "HEADERS", "FROM", "CASE", "WHEN", "THEN", "ELSE",
                 "END"}


def _outside_strings(stmt: str) -> str:
    return re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", stmt)


def _schema(script: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Labels -> properties and relationship types -> properties written by an export script."""
    labels: dict[str, set[str]] = {}
    rels: dict[str, set[str]] = {}
    for stmt in _statements(script):
        code = _outside_strings(stmt)
        if code.startswith("CREATE CONSTRAINT"):
            continue
        var: dict[str, tuple[dict, str]] = {}
        for v, lab, keys in re.findall(r"\((\w+):(\w+)(?:\s*\{([^}]*)\})?\)", code):
            labels.setdefault(lab, set()).update(k.strip().split(":")[0] for k in keys.split(",") if k.strip())
            var[v] = (labels, lab)
        for v, typ in re.findall(r"\[(\w*):(\w+)\]", code):
            rels.setdefault(typ, set())
            if v:
                var[v] = (rels, typ)
        for v, prop in re.findall(r"\b(\w+)\.(\w+)\s*=(?!=)", code.split(" SET ", 1)[1] if " SET " in code else ""):
            if v in var:
                d, name = var[v]
                d[name].add(prop)
    return labels, rels


def _doc_queries() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```cypher\n(.*?)```", text, flags=re.S)
    queries = [q.strip() for b in blocks for q in b.split(";") if q.strip()]
    # three analyst queries (query 3 in two parts) + proof queries P1-P5 (2026-11-22)
    assert len(blocks) == 8 and len(queries) == 9, "NEO4J.md: documented queries changed"
    return queries


def _doc_requirements() -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Labels with the properties used on variables bound to them, relationship types, and properties used on
    path elements (`n IN nodes(p)`, `r IN relationships(p)`): those must exist on some label / type."""
    need_labels: dict[str, set[str]] = {}
    need_types: set[str] = set()
    loose: set[str] = set()
    for q in _doc_queries():
        code = _outside_strings(q)
        bound: dict[str, str] = {}
        for v, lab, keys in re.findall(r"\((\w*):(\w+)(?:\s*\{([^}]*)\})?\)", code):
            need_labels.setdefault(lab, set()).update(k.strip().split(":")[0] for k in keys.split(",") if k.strip())
            if v:
                bound[v] = lab
        for types in re.findall(r"\[\w*:([\w|]+)[^\]]*\]", code):
            need_types.update(types.split("|"))
        need_types.update(re.findall(r"type\(\w+\)\s*=\s*'(\w+)'", q))
        for lab in re.findall(r"\b\w+:(\w+)\b(?!\s*[{)])", code):
            if lab[0].isupper():
                need_labels.setdefault(lab, set())
        for v, prop in re.findall(r"\b(\w+)\.(\w+)", code):
            if v in bound:
                need_labels[bound[v]].add(prop)
            else:
                loose.add(prop)
    return need_labels, need_types, loose


def _check_schema(labels: dict[str, set[str]], rels: dict[str, set[str]], where: str) -> None:
    need_labels, need_types, loose = _doc_requirements()
    for lab, props in need_labels.items():
        assert lab in labels, f"{where}: label {lab} missing"
        assert props <= labels[lab], f"{where}: {lab} lacks {props - labels[lab]}"
    assert need_types <= set(rels), f"{where}: relationship types {need_types - set(rels)} missing"
    have = set().union(*labels.values(), *rels.values())
    assert loose <= have, f"{where}: properties {loose - have} not produced"


# ------------------------------------------------------------------ tests


def test_api_cypher_is_the_cli_graph(run):
    run_id, client, cli, counts = run
    r = client.get("/v1/export/neo4j", params={"format": "cypher", "run_id": run_id})
    assert r.status_code == 200 and r.headers["content-disposition"].endswith(f'hydra-{run_id}.cypher"')
    script = r.text
    assert script == (cli / "graph.cypher").read_text(encoding="utf-8")  # API cypher = CLI graph.cypher
    rows: dict[str, list[dict]] = {}
    for stmt in _statements(script):
        if not stmt.startswith("UNWIND"):
            continue
        payload = stmt[len("UNWIND"):stmt.rindex("] AS r") + 1]
        code = _outside_strings(stmt)
        m = re.search(r"MERGE \(n:(\w+)", code) or re.search(r"\[x:(\w+)\]", code)
        rows.setdefault(m.group(1), []).extend(_literal(payload))
    by_label = {label: stem for stem, label, _k, _p in NODES} | {typ: stem for stem, typ, *_x in RELS}
    for name, stem in by_label.items():
        with (cli / f"{stem}.csv").open(encoding="utf-8") as fh:
            want = list(csv.DictReader(fh))
        got = rows.get(name, [])
        assert len(got) == len(want) == counts[stem], name
        key = next(iter(want[0])) if want else None
        if key:  # same first column (id / key / start node) values in the same order
            assert [str(x[key]) for x in got] == [w[key] for w in want], name
    assert counts["genes"] >= 2 and counts["has_gene"] >= 4 and counts["linked_to"] == N_INC * (N_INC - 1) // 2
    assert "evil-a.example" in {g["value"] for g in rows["Gene"]}
    assert all(isinstance(x["total_bits"], float) and isinstance(x["calibrated"], bool) for x in rows["LINKED_TO"])
    assert "x.bits = r.total_bits" in script
    assert "[x:LINKED]" not in script and "LINKED_TO" in script


def test_api_cypher_is_syntactically_sane(run):
    run_id, client, cli, _counts = run
    for script in (client.get("/v1/export/neo4j", params={"run_id": run_id}).text,
                   (cli / "import.cypher").read_text(encoding="utf-8")):
        stmts = _statements(script)
        assert stmts and all(s.endswith(";") for s in stmts)
        for s in stmts:
            words = set(re.findall(r"\b[A-Z][A-Z_]+\b", _outside_strings(s)))
            # labels/types are upper camel or upper snake: drop the known relationship types and labels
            words -= {typ for _s, typ, *_x in RELS}
            assert words <= KNOWN_CLAUSES, words - KNOWN_CLAUSES
            assert s.split()[0] in {"CREATE", "UNWIND", "LOAD"}


def test_documented_queries_match_both_exports(run):
    run_id, client, cli, _counts = run
    api = client.get("/v1/export/neo4j", params={"format": "cypher", "run_id": run_id}).text
    _check_schema(*_schema(api), "API cypher")
    _check_schema(*_schema((cli / "import.cypher").read_text(encoding="utf-8")), "CLI import.cypher")
    # the LOAD CSV script reads only columns that exist in the CSV files
    for stem, *_rest in (*NODES, *RELS):
        header = (cli / f"{stem}.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
        block = re.search(rf"FROM 'file:///{stem}\.csv' AS r(.*?);", (cli / "import.cypher").read_text(), re.S)
        assert set(re.findall(r"\br\.(\w+)", block.group(1))) <= set(header), stem


def test_api_zip_is_the_cli_folder_and_admin_csv(run):
    run_id, client, cli, counts = run
    for fmt in ("zip", "csv"):
        r = client.get("/v1/export/neo4j", params={"format": fmt, "run_id": run_id})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            got = {Path(n).name: zf.read(n) for n in zf.namelist()}
        assert got == {p.name: p.read_bytes() for p in cli.iterdir()}
    edges = list(csv.DictReader(io.StringIO(client.get("/v1/export/neo4j", params={"format": "edges_csv"}).text)))
    assert sum(e[":TYPE"] == "LINKED_TO" for e in edges) == counts["linked_to"]
    assert sum(e[":TYPE"] == "HAS_GENE" for e in edges) == counts["has_gene"]
    nodes = list(csv.DictReader(io.StringIO(client.get("/v1/export/neo4j", params={"format": "nodes_csv"}).text)))
    assert sum(n[":LABEL"] == "Gene" for n in nodes) == counts["genes"]
    ids = {n[":ID"] for n in nodes}
    assert {e[":START_ID"] for e in edges} | {e[":END_ID"] for e in edges} <= ids
    assert not list((client.app.state.hydra.settings.tmp_dir).glob("neo4j-*"))  # temp folder removed


def test_api_dataset_zip_equals_cli_folder(run, settings, tmp_path):
    run_id, client, _cli, _counts = run
    r = client.get("/v1/export/dataset", params={"run_id": run_id})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f"hydra-dataset-{run_id}.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        got = {Path(n).name: zf.read(n) for n in zf.namelist()}
    res = export_dataset(settings, run_id, tmp_path / "dataset-cli")
    cli = {p.name: p.read_bytes() for p in (tmp_path / "dataset-cli").iterdir()}
    assert set(got) == set(cli) and {"assignments.parquet", "assignments.csv", "manifest.json"} <= set(got)
    for name in ("incidents.csv", "assignments.csv", "campaigns.json", "README_DATASET.md"):
        assert got[name] == cli[name], name
    man = json.loads(got["manifest.json"])
    assert man["counts"] == res["counts"] and "assignments" in man["counts"]
    signed = {"SIGNED_MANIFEST.json", "SIGNED_MANIFEST.sig"}
    assert {f["name"] for f in man["files"]} == set(got) - {"manifest.json"} - signed
    asg = pd.read_parquet(io.BytesIO(got["assignments.parquet"]))
    assert {"duplicate_of", "new_pattern_id", "top_candidate", "top_bits", "reason_pl"} <= set(asg.columns)
    assert len(asg) == man["counts"]["assignments"]
    assert not list(settings.tmp_dir.glob("dataset-*"))  # streamed and cleaned
    from hydra.export_log import export_counts

    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert export_counts(conn, run_id)["by_kind"].get("dataset") == 1
    # signed package (spec test 7): API verify passes the download, fails one flipped byte; key shown in the UI
    key = client.get("/v1/exports/signing-key").json()
    assert key["fingerprint"] == r.headers["x-hydra-signing-key"]
    ok = client.post("/v1/exports/verify", content=r.content, params={"fingerprint": key["fingerprint"]}).json()
    assert ok["ok"] and ok["run_id"] == run_id and ok["files_checked"] == len(man["files"]) + 1, ok["errors"]
    assert ok["audit_chain_head"]["seq"] > 0
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(r.content)) as src, zipfile.ZipFile(buf, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            dst.writestr(n, data[:-1] + b"X" if n.endswith("/incidents.csv") else data)
    bad = client.post("/v1/exports/verify", content=buf.getvalue()).json()
    assert not bad["ok"] and bad["errors"] == ["hash nie pasuje / sha256 mismatch: incidents.csv"]


def test_api_dataset_404_without_run(settings):
    get_settings.cache_clear()
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/export/dataset").status_code == 404
        assert client.get("/v1/export/neo4j").status_code == 404
