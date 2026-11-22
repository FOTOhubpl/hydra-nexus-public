from __future__ import annotations

import hashlib
import json

import pandas as pd
from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.db import connect, migrate
from hydra.export_dataset import export_dataset

TOKEN = "EAAsecretTOKEN0123456789abcdefghijklmnopqrstuv"


def _db(settings):
    conn = connect(settings.db_path)
    migrate(conn)
    now = "2026-11-21T12:00:00+00:00"
    ex = conn.execute
    for i in ("t-1", "t-2", "t-3"):
        ex(
            "INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?)",
            (i, "test", f"/Users/jan/data/{i}", f"Title {i}", "2026-10-09T08:00:00", "SECRET-LABEL", "{}", now),
        )
        ex("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)", (f"a{i}", "text/html", 10, i, None, "{}", "stored", now))
        ex("INSERT INTO incident_artifacts VALUES (?,?,?,?,?)", (i, f"a{i}", "landing_html", "p.html", "{}"))
        for kind, val, fam, dg in (
            ("landing_host", "lure-fixture.live", "INF", "inf:operator:landing"),
            ("tracker_id", f"fb_pixel:1{i[-1]}", "INF", "inf:operator:tracker"),
            ("phrase", f"zarobek {TOKEN}", "TXT", None),
        ):
            ex(
                "INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence, "
                "extractor, "
                "dependency_group, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (i, fam, kind, val, 1.0, "observed", "OBS", json.dumps({"artifact_id": f"a{i}"}), "x@1", dg, now),
            )
    ex(
        "INSERT INTO runs VALUES (?,?,?,?,?,?)",
        (
            "r1",
            "analyse",
            "{}",
            json.dumps(
                {
                    "campaign_groups": [
                        {"id": "G-1", "label": "Grupa", "confidence": 0.5, "campaign_ids": ["C-1"], "shared_genes": []}
                    ]
                }
            ),
            now,
            now,
        ),
    )
    camp = {
        "id": "C-1",
        "label": "Kampania",
        "members": ["t-1", "t-2"],
        "scheme": "investment",
        "core_genome": [
            {
                "family": "INF",
                "kind": "landing_host",
                "value_a": "lure-fixture.live",
                "bits": 5.0,
                "explanation_pl": "wspólna domena",
            }
        ],
    }
    ex("INSERT INTO campaigns VALUES (?,?,?,?,?,?)", ("C-1", "r1", "Kampania", 0.9, "active", json.dumps(camp)))
    for m in ("t-1", "t-2"):
        ex("INSERT INTO campaign_members VALUES (?,?,?)", ("r1", "C-1", m))
    ex("INSERT INTO assignments VALUES (?,?,?,?,?,?)", ("t-3", "r1", "new_campaign", None, "{}", now))
    pair = {
        "family_bits": {"INF": 5.0},
        "reasons": [
            {
                "family": "INF",
                "kind": "landing_host",
                "value_a": "lure-fixture.live",
                "bits": 5.0,
                "explanation_pl": "wspólna domena",
            }
        ],
    }
    ex(
        "INSERT INTO pairs VALUES (?,?,?,?,?,?,?,?,?)",
        ("t-1", "t-2", "r1", 5.0, 0.9, 0, "same_campaign", "m@1", json.dumps(pair)),
    )
    for src, q, summ in (
        (
            "rdap",
            "lure-fixture.live",
            {"registrar": "Reg Inc", "registration": "2026-10-01", "nameservers": ["ns1.x-fixture.net"]},
        ),
        ("dns", "lure-fixture.live_A", {"answers": ["45.83.100.20"]}),
        ("asn", "45.83.100.20", {"asn": 64500, "as_name": "HOST-PL", "prefix": "45.83.100.0/24"}),
        (
            "crtsh",
            "lure-fixture.live",
            {"sans": ["lure-fixture.live", "sister-fixture.live"], "issuers": ["Let's Encrypt"]},
        ),
        ("cert_pl_list", "lure-fixture.live", {"host_listed": True, "insert_date": "2026-10-10"}),
    ):
        ex(
            "INSERT INTO enrichment VALUES (?,?,?,?,?,?,?,?)",
            (
                hashlib.sha256(f"{src}|{q}".encode()).hexdigest(),
                src,
                q,
                now,
                "observed",
                None,
                "/Users/jan/cache/x.json",
                json.dumps(summ),
            ),
        )
    conn.close()


def test_export_dataset(tmp_path, settings):
    _db(settings)
    out = tmp_path / "out"
    res = export_dataset(settings, "latest", out)
    assert res["run_id"] == "r1"
    c = res["counts"]
    assert c["incidents"] == 3 and c["genes"] == 9 and c["links"] == 1 and c["campaigns"] == 1
    assert c["campaign_groups"] == 1
    inc = pd.read_parquet(out / "incidents.parquet").set_index("incident_id")
    assert inc.loc["t-1", "campaign_id"] == "C-1" and inc.loc["t-1", "group_id"] == "G-1"
    assert inc.loc["t-3", "assignment_decision"] == "new_campaign"
    assert inc.loc["t-1", "genes_INF"] == 2 and inc.loc["t-1", "artifacts_landing_html"] == 1
    assert "label" not in inc.columns and (out / "incidents.csv").exists()
    genes = pd.read_parquet(out / "genes.parquet")
    assert set(genes["tier"].dropna()) == {"operator"}
    enr = pd.read_parquet(out / "enrichment.parquet").set_index("entity")
    row = enr.loc["lure-fixture.live"]
    assert row["registrar"] == "Reg Inc" and row["asns"] == "AS64500" and bool(row["cert_pl_listed"])
    assert str(enr["cert_pl_listed"].dtype) == "boolean"
    assert row["cert_san_siblings"] == "sister-fixture.live"
    links = pd.read_parquet(out / "links.parquet")
    assert "landing_host" in links.loc[0, "top_reasons"]
    camps = json.loads((out / "campaigns.json").read_text())
    assert camps[0]["infra_summary"]["landing_host"] == ["lure-fixture.live"]
    readme = (out / "README_DATASET.md").read_text()
    assert "NOT for public distribution" in readme
    man = json.loads((out / "manifest.json").read_text())
    names = {f["name"] for f in man["files"]}
    assert {
        "incidents.parquet",
        "genes.parquet",
        "enrichment.parquet",
        "links.parquet",
        "campaigns.json",
        "campaign_groups.json",
        "README_DATASET.md",
        "incidents.csv",
    } <= names
    for f in man["files"]:
        assert hashlib.sha256((out / f["name"]).read_bytes()).hexdigest() == f["sha256"]
    # no secrets, user names or ground-truth labels anywhere
    for p in out.iterdir():
        b = p.read_bytes()
        assert TOKEN.encode() not in b and b"SECRET-LABEL" not in b and b"/Users/jan" not in b, p.name


def test_assignments_table_with_duplicates_and_new_patterns(tmp_path, settings):
    """B17/B19: newest assignment per incident across the run family; `duplicate_of` / `new_pattern_id` come from
    `Assignment.meta` and are null when absent."""
    _db(settings)
    conn = connect(settings.db_path)
    later = "2026-11-21T13:00:00+00:00"
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                 ("a1", "assign", json.dumps({"base_run": "r1"}), "{}", later, later))
    conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                 ("a-other", "assign", json.dumps({"base_run": "other"}), "{}", later, later))
    dup = {"incident_id": "t-2", "decision": "assign", "campaign_id": "C-1", "reason_pl": f"duplikat {TOKEN}",
           "model": "assign@1", "candidates": [{"campaign_id": "C-1", "probability": 0.97, "support": 2,
                                                "best_pair": {"total_bits": 42.5, "hypothesis": "same_campaign"}}],
           "meta": {"duplicate_of": "t-1"}}
    new = {"incident_id": "t-3", "decision": "new_campaign", "campaign_id": None, "reason_pl": "nowy wzorzec",
           "model": "assign@1", "candidates": [], "meta": {"new_pattern": {"id": "P-7", "members": ["t-3"]}}}
    for d in (dup, new):
        conn.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?)",
                     (d["incident_id"], "a1", d["decision"], d["campaign_id"], json.dumps(d), later))
    conn.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?)",  # other run family: never exported
                 ("t-1", "a-other", "abstain", None, json.dumps({"meta": {"duplicate_of": "x"}}), later))
    conn.commit()
    conn.close()
    out = tmp_path / "out"
    res = export_dataset(settings, "r1", out)
    asg = pd.read_parquet(out / "assignments.parquet").set_index("incident_id")
    assert list(asg.index) == ["t-2", "t-3"]  # t-3: the newer a1 row replaces the r1 row; t-1: none in the family
    assert asg.loc["t-2", "duplicate_of"] == "t-1" and pd.isna(asg.loc["t-2", "new_pattern_id"])
    assert asg.loc["t-2", "top_candidate"] == "C-1" and asg.loc["t-2", "top_bits"] == 42.5
    assert asg.loc["t-3", "new_pattern_id"] == "P-7" and pd.isna(asg.loc["t-3", "duplicate_of"])
    assert asg.loc["t-3", "run_id"] == "a1" and TOKEN not in asg.loc["t-2", "reason_pl"]
    csv_rows = pd.read_csv(out / "assignments.csv").set_index("incident_id")
    assert list(csv_rows.columns) == list(asg.columns)
    c = res["counts"]
    assert c["assignments"] == 2 and c["duplicates"] == 1 and c["new_patterns"] == 1
    inc = pd.read_parquet(out / "incidents.parquet").set_index("incident_id")
    assert inc.loc["t-2", "assignment_decision"] == "assign"
    man = json.loads((out / "manifest.json").read_text())
    assert {"assignments.parquet", "assignments.csv"} <= {f["name"] for f in man["files"]}
    assert man["counts"]["assignments"] == 2


def test_assignments_columns_null_when_meta_absent(tmp_path, settings):
    _db(settings)
    out = tmp_path / "out"
    export_dataset(settings, "latest", out)
    asg = pd.read_parquet(out / "assignments.parquet")
    assert list(asg["incident_id"]) == ["t-3"]
    assert asg["duplicate_of"].isna().all() and asg["new_pattern_id"].isna().all()


def test_export_dataset_scoped_to_one_campaign(tmp_path, settings):
    """AUDIT B15: the evidence package for `campaign_id` holds that campaign only (t-3 is outside it)."""
    import pytest

    from hydra.evidence_sign import verify_package

    _db(settings)
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                 ("t-9", "test", "t-9", "Title t-9", None, None, "{}", "2026-11-21T13:00:00+00:00"))
    conn.execute("INSERT INTO assignments VALUES (?,?,?,?,?,?)",
                 ("t-9", "r1", "assign", "C-1", "{}", "2026-11-21T13:00:00+00:00"))
    conn.commit()
    conn.close()
    out = tmp_path / "camp"
    res = export_dataset(settings, "latest", out, campaign_id="C-1")
    c = res["counts"]
    assert c["campaigns"] == 1 and c["links"] == 1 and c["campaign_groups"] == 1
    inc = set(pd.read_parquet(out / "incidents.parquet")["incident_id"])
    assert {"t-1", "t-2"} <= inc and "t-3" not in inc
    assert set(pd.read_parquet(out / "assignments.parquet")["incident_id"]) == {"t-9"}
    assert json.loads((out / "manifest.json").read_text())["run"]["campaign_id"] == "C-1"
    assert verify_package(out)["ok"], verify_package(out)
    with pytest.raises(KeyError):
        export_dataset(settings, "latest", tmp_path / "none", campaign_id="C-missing")
