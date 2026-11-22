from __future__ import annotations

import json

import pytest
from fixtures import _synthetic_warninglists, web_incident  # noqa: F401

from hydra.enrich import Enricher, cert_pl, client, records_for
from hydra.genome import base
from hydra.genome.inf import inf_enrich

RDAP = {
    "events": [{"eventAction": "registration", "eventDate": "2026-10-01T10:00:00Z"}],
    "nameservers": [{"ldhName": "NS1.SHADY-DNS-FIXTURE.NET"}, {"ldhName": "ns2.shady-dns-fixture.net"}],
    "entities": [
        {
            "roles": ["registrar"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]],
            "publicIds": [{"type": "IANA Registrar ID", "identifier": "9999"}],
        }
    ],
}
CRT = [
    {
        "issuer_name": "C=US, O=Let's Encrypt, CN=E6",
        "name_value": "news-247daily-fixture.live\nsister-brand-fixture.live",
        "not_before": "2026-10-02T00:00:00",
    }
]


def _put(root, source, name, status, data):
    p = root / source / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"source": source, "key": name, "fetched_at": "2026-11-21T12:00:00+00:00", "status": status, "data": data}
        )
    )


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "enrich"
    _put(root, "rdap", "news-247daily-fixture.live", 200, RDAP)
    _put(root, "crtsh", "news-247daily-fixture.live", 200, CRT)
    _put(root, "dns", "www.news-247daily-fixture.live_A", "ok", {"answers": ["45.83.100.20"]})
    _put(root, "dns", "news-247daily-fixture.live_MX", "ok", {"answers": ["10 mx.mailer-fixture.net."]})
    _put(
        root,
        "dns",
        "20.100.83.45.origin.asn.cymru.com_TXT",
        "ok",
        {"answers": ['"64500 | 45.83.100.0/24 | PL | ripencc | 2020-01-01"']},
    )
    _put(
        root,
        "dns",
        "as64500.asn.cymru.com_TXT",  # dns.query lowercases names; cache keys are case-sensitive on Linux
        "ok",
        {"answers": ['"64500 | PL | ripencc | 2001 | EXAMPLE-HOST, PL"']},
    )
    return root


@pytest.fixture
def certpl(tmp_path, monkeypatch):
    d = tmp_path / "cert"
    d.mkdir()
    (d / "domains.json").write_text(
        json.dumps(
            [
                {
                    "RegisterPositionId": 1,
                    "DomainAddress": "www.news-247daily-fixture.live",
                    "InsertDate": "2026-10-10T00:00:00",
                    "DeleteDate": None,
                },
                {
                    "RegisterPositionId": 2,
                    "DomainAddress": "netlify.app",
                    "InsertDate": "2026-01-01",
                    "DeleteDate": None,
                },
            ]
        )
    )
    (d / "SNAPSHOT.json").write_text(
        json.dumps({"snapshot": "test", "retrieved_at": "2026-11-20T00:00:00+00:00", "sha256": "0" * 64})
    )
    monkeypatch.setenv("HYDRA_CERT_PL_DIR", str(d))
    cert_pl._load.cache_clear()
    yield d
    cert_pl._load.cache_clear()


def _no_network(*a, **k):
    raise AssertionError("replay mode must not fetch")


def test_replay_genes_and_records(tmp_path, settings, cache, certpl, monkeypatch):
    monkeypatch.setattr(client, "http_json", _no_network)
    enr = Enricher(cache_dir=cache, live=False)
    inc = web_incident(tmp_path / "inc")
    ctx = base.ExtractContext(settings=settings, work_dir=tmp_path, enrich=enr)
    res = inf_enrich(inc, ctx)
    kv = {(g.kind, g.value) for g in res.genes}
    assert ("registrar", "Example Registrar") in kv
    assert ("registration_day", "2026-10-01") in kv
    assert ("registrar_day", "Example Registrar|2026-10-01") in kv
    assert ("ns_host", "ns1.shady-dns-fixture.net") in kv
    assert ("mx_host", "mx.mailer-fixture.net") in kv
    assert ("dns_ip", "45.83.100.20") in kv
    assert ("asn", "AS64500") in kv and ("as_org", "EXAMPLE-HOST, PL") in kv
    assert ("cert_san_sibling", "sister-brand-fixture.live") in kv
    assert ("cert_issuer", "Let's Encrypt") in kv
    assert ("cert_pl_listed_host", "www.news-247daily-fixture.live") in kv
    assert ("domain_age_at_observation", "<30d") in kv
    listed = next(g for g in res.genes if g.kind == "cert_pl_listed_host")
    assert listed.dependency_group == "inf:label:cert_pl"
    # the uncached host (click.tds-hop-fixture.top) is unavailable, not an error
    recs = records_for(inc, enr)
    by = {(r.source, r.query): r for r in recs}
    assert by[("rdap", "news-247daily-fixture.live")].status == "observed"
    assert by[("rdap", "tds-hop-fixture.top")].status == "unavailable"
    assert by[("cert_pl_list", "www.news-247daily-fixture.live")].summary["host_listed"] is True
    keys = {r.key for r in recs}
    assert all(g.evidence.enrichment_key in keys for g in res.genes if g.evidence.enrichment_key)


def test_live_fetch_is_cached_and_errors_are_not(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch
    enr = Enricher(cache_dir=tmp_path / "c", live=True)
    monkeypatch.setitem(client.DELAY, "rdap", 0)
    calls = []

    def ok():
        calls.append(1)
        return "https://rdap.example/x", "200", {"events": []}

    r1 = enr.get("rdap", "x.example", ok)
    r2 = enr.get("rdap", "x.example", ok)
    assert r1.ok and r2.ok and len(calls) == 1 and r2.cache_path is not None
    bad = enr.get("rdap", "y.example", lambda: (None, "error:http503", None))
    assert bad.gene_status == "unavailable"
    assert (tmp_path / "c" / "rdap" / "y.example.error.json").exists()
    assert not (tmp_path / "c" / "rdap" / "y.example.json").exists()


def test_no_hosts_is_not_applicable(tmp_path, settings):
    from hydra.contracts import Incident

    inc = base.ResolvedIncident(Incident(id="e", source="t", source_ref="t"), [])
    res = inf_enrich(inc, base.ExtractContext(settings=settings, work_dir=tmp_path))
    assert [g.status for g in res.genes] == ["not_applicable"]


def test_mixed_naive_and_aware_dates(tmp_path, settings, cache, certpl):
    from hydra.genome.inf import _dt, age_bucket

    a, b = _dt("2026-10-09T08:00:00"), _dt("2026-10-01T10:00:00+02:00")
    assert a.tzinfo is not None and b.tzinfo is not None
    assert age_bucket((a - b).total_seconds() / 86400) == "<30d"
    assert _dt("2026-10-01") is not None and _dt("garbage") is None
    # naive observed_at with aware RDAP date: no TypeError, age gene present
    inc = web_incident(tmp_path / "inc2")
    inc.incident.observed_at = "2026-10-09T08:00:00"
    res = inf_enrich(
        inc, base.ExtractContext(settings=settings, work_dir=tmp_path, enrich=Enricher(cache_dir=cache, live=False))
    )
    assert ("domain_age_at_observation", "<30d") in {(g.kind, g.value) for g in res.genes}
    assert all(g.status != "failed" for g in res.genes)
