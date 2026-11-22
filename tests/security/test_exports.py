"""Independent STIX validation, graph import consistency and export-injection regressions."""

import csv
import io
import json
import re
import uuid
from contextlib import closing
from datetime import datetime

import pytest
import stix2
from fastapi.testclient import TestClient
from stix2.exceptions import InvalidValueError

from hydra.api import create_app
from hydra.db import Writer, connect
from hydra.export_abuse import abuse_package, validate_xarf
from hydra.export_dataset import export_dataset
from hydra.export_neo4j import export_run
from hydra.export_stix import export_stix
from tests.security.cypher import statements
from tests.security.helpers import STAMP, seed


def test_stix_bundle_ids_references_dates_and_parse_round_trip(settings):
    campaign, run, _ = seed(settings)
    bundle = export_stix(settings, campaign.id, run)
    parsed = stix2.parse(json.dumps(bundle), version="2.1", allow_custom=True)
    assert json.loads(parsed.serialize()) == bundle
    ids = {obj["id"] for obj in bundle["objects"]}
    assert len(ids) == len(bundle["objects"])
    for obj in bundle["objects"]:
        kind, identifier = obj["id"].split("--", 1)
        assert kind == obj["type"]
        assert str(uuid.UUID(identifier)) == identifier
        for key, value in obj.items():
            if key.endswith("_ref"):
                assert value in ids, (key, value)
            elif key.endswith("_refs"):
                assert set(value) <= ids
            if key in {"created", "modified", "first_seen", "last_seen", "valid_from",
                       "first_observed", "last_observed"}:
                assert datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
        if "created" in obj:
            assert obj["modified"] >= obj["created"]
    assert any(obj["type"] == "relationship" and obj["relationship_type"] == "indicates"
               for obj in bundle["objects"])
    assert export_stix(settings, campaign.id, run) == bundle


def test_stix_library_rejects_invalid_identifier(settings):
    campaign, run, _ = seed(settings)
    bundle = export_stix(settings, campaign.id, run)
    bundle["objects"][0]["id"] = "identity--not-a-uuid"
    with pytest.raises(InvalidValueError):
        stix2.parse(json.dumps(bundle), version="2.1", allow_custom=True)


def test_stix_preserves_source_timestamp_offset(settings):
    source = "2026-11-21T18:00:00+02:00"
    campaign, run, _ = seed(settings, date=source)
    bundle = export_stix(settings, campaign.id, run)
    exported = next(obj for obj in bundle["objects"] if obj["type"] == "campaign")
    assert datetime.fromisoformat(exported["first_seen"].replace("Z", "+00:00")) == datetime.fromisoformat(source)


def test_stix_url_pattern_escapes_quotes(settings):
    campaign, run, _ = seed(settings)
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda conn: conn.execute(
            "INSERT INTO genes(incident_id,family,kind,value,status,provenance,extractor,created_at)"
            " VALUES ('astra-a','INF','entry_url',?,'observed','OBS','astra-synthetic@1',?)",
            ("https://campaign.example/path'quote", STAMP)))
    finally:
        writer.close()
    bundle = export_stix(settings, campaign.id, run)
    assert any("quote" in obj.get("pattern", "") for obj in bundle["objects"])
    stix2.parse(json.dumps(bundle), version="2.1", allow_custom=True)


def test_neo4j_csv_graph_has_unique_keys_and_complete_endpoints(settings, tmp_path):
    _, run, _ = seed(settings)
    out = tmp_path / "neo4j"
    with closing(connect(settings.db_path, readonly=True)) as conn:
        counts = export_run(conn, run, out)
    tables = {p.stem: list(csv.DictReader(io.StringIO(p.read_text()))) for p in out.glob("*.csv")}
    assert counts == {name: len(rows) for name, rows in tables.items()}
    node_keys = {}
    for table, key in [("incidents", "id"), ("campaigns", "id"), ("groups", "id"), ("genes", "key")]:
        node_keys[table] = {row[key] for row in tables[table]}
        assert len(node_keys[table]) == counts[table]
    for row in tables["member_of"]:
        assert row["incident_id"] in node_keys["incidents"] and row["campaign_id"] in node_keys["campaigns"]
    for row in tables["in_group"]:
        assert row["campaign_id"] in node_keys["campaigns"] and row["group_id"] in node_keys["groups"]
    for row in tables["has_gene"]:
        assert row["incident_id"] in node_keys["incidents"] and row["gene_key"] in node_keys["genes"]
    for row in tables["linked_to"]:
        assert {row["a"], row["b"]} <= node_keys["incidents"]
    script = (out / "import.cypher").read_text()
    assert set(re.findall(r"file:///([a-z_]+)\.csv", script)) == set(tables)
    parsed = statements(script)
    assert len(parsed) == 12
    assert sum(statement.startswith("LOAD CSV") for statement in parsed) == 8
    assert sum(len(v) for v in node_keys.values()) == 6
    assert sum(counts[k] for k in ("member_of", "in_group", "has_gene", "linked_to")) == 8


def test_neo4j_distinct_long_gene_values_do_not_merge(settings, tmp_path):
    _, run, _ = seed(settings, value="A" * 450 + "first")
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda conn: conn.execute(
            "INSERT INTO genes(incident_id,family,kind,value,status,provenance,extractor,created_at)"
            " SELECT id,'INF','operator_id',?,'observed','OBS','astra-synthetic@1',? FROM incidents",
            ("A" * 450 + "second", STAMP)))
    finally:
        writer.close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        counts = export_run(conn, run, tmp_path / "neo4j")
    rows = list(csv.DictReader(io.StringIO((tmp_path / "neo4j/genes.csv").read_text())))
    assert counts["genes"] == 3
    assert len({row["key"] for row in rows}) == 3


@pytest.mark.parametrize("endpoint", ["neo4j", "subgraph"])
def test_cypher_quotes_and_semicolons_stay_inside_data(settings, endpoint):
    payload = 'title\'; CREATE (:AstraInjected); // "quoted" \\ newline\nnext'
    _, run, _ = seed(settings, title=payload)
    with TestClient(create_app(settings)) as client:
        if endpoint == "neo4j":
            response = client.get("/v1/export/neo4j", params={"run_id": run})
        else:
            response = client.post("/v1/graph/export", json={"run_id": run, "ids": ["astra-a", "astra-b"],
                                                           "format": "cypher"})
    assert response.status_code == 200
    parsed = statements(response.text)
    # merge-batch3: both endpoints render the one graph model of hydra/export_neo4j.py (feat/variants-pipe B19):
    # 4 constraints, then one `UNWIND [rows] AS r MERGE/MATCH ...` statement per non-empty node/relationship table
    # (incidents 2 rows, campaigns, groups, genes; member_of, in_group, has_gene, linked_to).
    assert len(parsed) == 12
    assert not any("AstraInjected" in statement for statement in parsed)
    assert all(s.startswith(("CREATE CONSTRAINT ", "UNWIND [")) for s in parsed)
    assert sum(bool(re.search(r"\bAS r\nMERGE \(\w+:(Incident|Campaign) ", s)) for s in parsed) == 2
    assert sum(bool(re.search(r"\bAS r\nMATCH ", s)) for s in parsed) == 4
    assert json.dumps(payload) in response.text  # the hostile title survives intact inside one escaped literal


@pytest.mark.parametrize("target", ["files", "api", "subgraph"])
def test_run_identifier_cannot_inject_cypher_outside_comment(settings, tmp_path, target):
    _, run, _ = seed(settings, run_id="run\nCREATE (:AstraInjected);\n//")
    if target == "files":
        with closing(connect(settings.db_path, readonly=True)) as conn:
            export_run(conn, run, tmp_path / "neo4j")
        script = (tmp_path / "neo4j/import.cypher").read_text()
    else:
        with TestClient(create_app(settings)) as client:
            if target == "api":
                response = client.get("/v1/export/neo4j", params={"run_id": run})
            else:
                response = client.post("/v1/graph/export", json={"run_id": run, "ids": ["astra-a", "astra-b"],
                                                               "format": "cypher"})
        assert response.status_code == 200
        script = response.text
    assert not any("AstraInjected" in statement for statement in statements(script))


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
@pytest.mark.parametrize("target", ["neo4j-files", "dataset", "campaign-csv", "neo4j-api"])
def test_untrusted_csv_cells_do_not_start_spreadsheet_formulas(settings, tmp_path, prefix, target):
    payload = prefix + "SUM(1,1)"
    campaign, run, _ = seed(settings, title=payload, value=payload)
    if target == "neo4j-files":
        with closing(connect(settings.db_path, readonly=True)) as conn:
            export_run(conn, run, tmp_path / "neo4j")
        text, column = (tmp_path / "neo4j/incidents.csv").read_text(), "title"
    elif target == "dataset":
        export_dataset(settings, run, tmp_path / "dataset")
        text, column = (tmp_path / "dataset/incidents.csv").read_text(), "title"
    else:
        with TestClient(create_app(settings)) as client:
            response = client.get(
                f"/v1/campaigns/{campaign.id}/export" if target == "campaign-csv" else "/v1/export/neo4j",
                params={"run_id": run, "format": "csv" if target == "campaign-csv" else "nodes_csv"})
        assert response.status_code == 200
        text, column = response.text, "value" if target == "campaign-csv" else "title"
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows
    assert all(not row[column].lstrip().startswith(("=", "+", "-", "@")) for row in rows), (target, prefix)


def test_xarf_newlines_are_escaped_json_data_and_evidence_hashes_match(settings):
    import zipfile

    campaign, run, _ = seed(settings, title='First line\n"reporter": "injected"\r\nLast line')
    data, manifest = abuse_package(settings, campaign.id, run)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        reports = [json.loads(archive.read(name)) for name in archive.namelist() if name.endswith(".xarf.json")]
    assert len(reports) == 1 and manifest["sent"] is False
    for report in reports:
        assert isinstance(report["reporter"], dict)
        assert "\n" in report["description"]
        assert validate_xarf(report) == []


@pytest.mark.parametrize(("field", "bad"), [
    ("timestamp", "2026-11-21"),
    ("reporter", []),
    ("evidence", [{"content_type": "text/plain", "payload": "", "size": "NaN"}]),
])
def test_xarf_subset_validator_rejects_malformed_input_without_crashing(settings, field, bad):
    import zipfile

    campaign, run, _ = seed(settings)
    data, _ = abuse_package(settings, campaign.id, run)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        report = json.loads(archive.read("reports/campaign.example.xarf.json"))
    report[field] = bad
    # A validator must return validation errors, not accept an invalid timestamp or raise TypeError/ValueError.
    try:
        errors = validate_xarf(report)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AssertionError(f"Validator crashed: {type(exc).__name__}") from exc
    assert errors
