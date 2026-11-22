"""Payload builders on the synthetic run: schemas, line structure, SQL loads, indicators only."""

from __future__ import annotations

import csv
import io
import json
import sqlite3

import pytest
from ui_api.synthetic import seed

from hydra.integrations import exports

FORBIDDEN_SNIPPETS = ("Fałszywy artykuł", "Cudowny lek", "Kasyno online", "prezenter", ".png", "/Users/", "\x89PNG")


@pytest.fixture
def seeded(settings):
    seed(settings.db_path, settings.cas_dir)
    return settings


@pytest.fixture
def col(seeded):
    return exports.collect(seeded)


def test_collect_indicators_only(col):
    assert col.run_id == "syn-run-1" and col.indicators
    for i in col.indicators:
        assert set(i) == set(exports.INDICATOR_FIELDS)
        assert i["type"] in ("domain", "url", "ip", "sha256")
    for c in col.campaigns:
        assert set(c) == set(exports.CAMPAIGN_FIELDS)


@pytest.mark.parametrize("fmt", sorted(exports.BUILDERS))
def test_no_contents_titles_or_paths(col, fmt):
    p = exports.BUILDERS[fmt](col)
    text = p.body.decode()
    for s in FORBIDDEN_SNIPPETS:
        assert s not in text, (fmt, s)
    assert p.sha256 and len(p.sha256) == 64 and p.fields_sent
    assert json.dumps(p.preview, ensure_ascii=False, default=str)


def test_unknown_run_and_campaign(seeded):
    with pytest.raises(KeyError):
        exports.collect(seeded, run_id="nope")
    with pytest.raises(KeyError):
        exports.collect(seeded, campaign_id="nope")


def test_misp_event_schema(col):
    ev = json.loads(exports.misp_event(col).body)["Event"]
    assert set(ev) >= {"info", "threat_level_id", "analysis", "distribution", "Attribute", "Tag"}
    assert ev["distribution"] == "0" and {"name": "tlp:amber"} in ev["Tag"]
    assert ev["Attribute"]
    for a in ev["Attribute"]:
        assert set(a) == {"type", "category", "value", "to_ids", "comment"}
        assert a["type"] in ("domain", "url", "ip-dst", "sha256")
        assert a["category"] in ("Network activity", "Payload delivery")
        assert isinstance(a["to_ids"], bool)


def test_ndjson_lines(col):
    body = exports.ndjson(col).body.decode()
    assert body.endswith("\n")
    lines = body.splitlines()
    assert len(lines) == len(col.indicators)
    for line in lines:
        d = json.loads(line)
        assert d["indicator"]["type"] and d["indicator"]["value"] and d["source"] == "hydra-nexus"


def test_es_bulk_action_and_doc_pairs(col):
    body = exports.es_bulk(col).body.decode()
    assert body.endswith("\n")
    lines = body.splitlines()
    assert len(lines) == 2 * len(col.indicators)
    for action, doc in zip(lines[::2], lines[1::2], strict=True):
        a = json.loads(action)
        assert list(a) == ["index"] and a["index"]["_index"] == exports.ES_INDEX and a["index"]["_id"]
        assert "indicator" in json.loads(doc)


def test_splunk_hec_schema(col):
    body = exports.siem_json(col).body.decode()
    dec = json.JSONDecoder()
    pos, events = 0, []
    while pos < len(body):  # HEC batch: concatenated JSON objects
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            break
        obj, pos = dec.raw_decode(body, pos)
        events.append(obj)
    assert len(events) == len(col.indicators)
    for e in events:
        assert set(e) == {"time", "host", "source", "sourcetype", "event"}
        assert e["host"] == "hydra-nexus" and e["source"] == "hydra" and e["sourcetype"] == "hydra:indicator"
        assert isinstance(e["time"], float | int) and isinstance(e["event"], dict)


def test_sql_loads_into_sqlite(col):
    text = exports.sql(col).body.decode()
    db = sqlite3.connect(":memory:")
    db.executescript(text)
    assert db.execute("SELECT COUNT(*) FROM hydra_indicators").fetchone()[0] == len(col.indicators)
    assert db.execute("SELECT COUNT(*) FROM hydra_campaigns").fetchone()[0] == len(col.campaigns)


def test_sql_quotes_hostile_values():
    col = exports.Collected("r'1", None, [], [{"type": "url", "value": "http://x.example/'); DROP TABLE x;--",
                                                "campaign_id": "c", "decision": "active", "score_bits": 1.5,
                                                "count": 2, "first_seen": None, "last_seen": None}])
    db = sqlite3.connect(":memory:")
    db.executescript(exports.sql(col).body.decode())
    assert db.execute("SELECT value FROM hydra_indicators").fetchone()[0].endswith("DROP TABLE x;--")


def test_csv_formula_safe():
    col = exports.Collected("r", None, [], [{"type": "domain", "value": "=cmd.example", "campaign_id": "c",
                                              "decision": "active", "score_bits": None, "count": 1,
                                              "first_seen": None, "last_seen": None}])
    rows = list(csv.reader(io.StringIO(exports.indicators_csv(col).body.decode())))
    assert rows[0] == exports.INDICATOR_FIELDS and not rows[1][1].startswith("=")


def test_url_query_secrets_redacted(seeded, monkeypatch):
    from hydra.campaign_facts import campaign_hosts as real

    def fake(view):
        hs = real(view)
        for h in hs:
            h["urls"] = [f"https://{h['host']}/land?access_token=EAAB1234567890abcdefghijklmnopqrstuvwxyz&x=1"]
        return hs

    monkeypatch.setattr("hydra.campaign_facts.campaign_hosts", fake)
    col = exports.collect(seeded)
    body = exports.ndjson(col).body.decode()
    assert "EAAB1234567890" not in body and col.redactions


@pytest.mark.parametrize("fmt", ["stix21", "neo4j", "evidence"])
def test_wrapped_formats(seeded, fmt):
    p = exports.build(seeded, fmt)
    assert p.body and p.content_type == exports.CONTENT_TYPES[fmt]


def test_xarf_needs_campaign(seeded):
    with pytest.raises(ValueError):
        exports.build(seeded, "xarf")
    assert exports.build(seeded, "xarf", campaign_id="syn-c1").body[:2] == b"PK"
