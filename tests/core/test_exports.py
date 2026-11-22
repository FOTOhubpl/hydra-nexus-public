"""Abuse package (XARF v4), STIX 2.1, lifecycle and pivot queries on the synthetic corpus + a synthetic replay cache."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from hydra.config import Settings
from hydra.db import Writer
from hydra.pipeline import analyse
from tests.core.test_pipeline import corpus, test_extractors  # noqa: F401 — fixture

XARF_SAMPLES = Path.home() / "HydraNexus" / "cache" / "xarf-spec" / "samples" / "v4"


def seed_cache(settings: Settings) -> None:
    base = Path(settings.enrich_cache)
    rdap = {"events": [{"eventAction": "registration", "eventDate": "2026-09-01T10:00:00Z"}],
            "nameservers": [{"ldhName": "ns1.reg.example"}],
            "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["fn", {}, "text", "Reg Inc."]]],
                          "entities": [{"roles": ["abuse"], "vcardArray": ["vcard", [
                              ["fn", {}, "text", "Abuse Desk"], ["email", {}, "text", "abuse@reg.example"]]]}]}]}
    crt = [{"name_value": "evil-a.example", "issuer_name": "C=US, O=Let's Encrypt",
            "not_before": "2026-09-03T00:00:00"},
           {"name_value": "www.evil-a.example", "issuer_name": "O=Let's Encrypt", "not_before": "2026-10-01T00:00:00"}]
    for src, q, data in (("rdap", "evil-a.example", rdap), ("crtsh", "evil-a.example", crt)):
        p = base / src / f"{q}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"source": src, "query": q, "fetched_at": "2026-11-21T12:00:00+00:00",
                                 "status": "200", "data": data}))


@pytest.fixture
def built(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    seed_cache(settings)
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    w = Writer(settings.db_path)
    try:
        w.run(lambda c: c.execute("UPDATE incidents SET observed_at='2026-10-05' WHERE id IN ('nask-1','nask-2')"))
    finally:
        w.close()
    camp = next(c for c in res.clustering.campaigns if "nask-1" in c.members)
    return settings, res, camp


def test_abuse_package_is_valid_xarf_and_never_sent(built):
    from hydra.export_abuse import abuse_package, validate_xarf

    settings, _res, camp = built
    data, manifest = abuse_package(settings, camp.id)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert {"manifest.json", "CZYTAJ_MNIE.txt", "reports/evil-a.example.xarf.json", "notes/evil-a.example.txt"} \
        <= names
    assert manifest["sent"] is False and manifest["hosts"] == 1 and manifest["abuse_contact_coverage"] == 1.0
    rep = json.loads(zf.read("reports/evil-a.example.xarf.json"))
    assert validate_xarf(rep) == [] and rep["type"] == "fraud" and rep["fraud_type"] == "investment"
    assert rep["registrar"] == "Reg Inc." and rep["domain"] == "evil-a.example"
    assert any(e["content_type"] == "text/html" for e in rep["evidence"])
    note = zf.read("notes/evil-a.example.txt").decode()
    assert "abuse@reg.example" in note and "nie został nigdzie wysłany" in note
    bad = {**rep, "report_id": "x", "evidence": [{"content_type": "text/plain", "payload": "!!"}]}
    assert {"report_id not uuid", "evidence[0] payload not base64"} <= set(validate_xarf(bad))


@pytest.mark.skipif(not XARF_SAMPLES.is_dir(), reason="XARF v4 samples not downloaded")
def test_validator_accepts_official_xarf_samples():
    from hydra.export_abuse import validate_xarf

    for name in ("content-phishing.json", "content-fraud.json"):
        s = json.loads((XARF_SAMPLES / name).read_text())
        errs = [e for e in validate_xarf(s) if "hash mismatch" not in e and "url" != e]  # samples use dummy payloads
        assert errs == [], (name, errs)


def test_stix_bundle_validates_and_is_deterministic(built):
    import stix2

    from hydra.export_stix import export_stix

    settings, res, camp = built
    b1 = export_stix(settings, camp.id)
    b2 = export_stix(settings, camp.id)
    assert b1 == b2 and b1["type"] == "bundle"
    types = {o["type"] for o in b1["objects"]}
    assert {"campaign", "indicator", "infrastructure", "observed-data", "relationship", "domain-name",
            "identity"} <= types
    pats = [o["pattern"] for o in b1["objects"] if o["type"] == "indicator"]
    assert "[domain-name:value = 'evil-a.example']" in pats
    stix2.parse(json.dumps(b1), allow_custom=True)
    run_bundle = export_stix(settings, None, res.run_id)
    assert sum(o["type"] == "campaign" for o in run_bundle["objects"]) == 2  # multi-member campaigns


def test_lifecycle_events_and_lead_times(built):
    from hydra.lifecycle import campaign_lifecycle, corpus_lead_times

    settings, res, camp = built
    lc = campaign_lifecycle(settings, camp.id)
    d = lc["domains"][0]
    kinds = [e["type"] for e in d["events"]]
    assert kinds == ["registration", "first_certificate", "last_certificate", "first_observed", "last_seen"]
    assert d["lead_days"]["registration->first_observed"] == pytest.approx(33.58, abs=0.01)
    assert d["lead_days"]["first_certificate->first_observed"] == 32.0
    assert all(e["source"] for e in d["events"])
    lt = corpus_lead_times(settings, res.run_id)
    assert lt["lead_times"]["registration->first_observed"]["n"] == 1


def test_pivot_queries_are_strings_only(built):
    from hydra.pivot_queries import campaign_pivots

    settings, _res, camp = built
    p = campaign_pivots(settings, camp.id)
    flat = [x["query"] for v in p["queries"].values() for x in v]
    assert "%.evil-a.example" in flat and "domain:evil-a.example" in flat and "hostname:evil-a.example" in flat
    assert "https://rdap.org/domain/evil-a.example" in flat and p["note_pl"]


def test_export_endpoints(built):
    from fastapi.testclient import TestClient

    from hydra.api import create_app

    settings, res, camp = built
    with TestClient(create_app(settings)) as client:
        z = client.get(f"/v1/campaigns/{camp.id}/abuse-package")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        for path in (f"/v1/campaigns/{camp.id}/stix", f"/v1/campaigns/{camp.id}/lifecycle",
                     f"/v1/campaigns/{camp.id}/pivots", f"/v1/runs/{res.run_id}/stix",
                     f"/v1/runs/{res.run_id}/lead-times"):
            assert client.get(path).status_code == 200, path
        assert client.get("/v1/campaigns/C-nope/stix").status_code == 404


def test_xarf_url_comes_from_observed_urls(tmp_path):
    """CR-E2E-3: the normalised entry_url gene has no scheme; the XARF url is the observed https URL (HAR entry,
    then the saved-from comment), and `http://host/` is only the fallback when nothing was observed."""
    from hydra.export_abuse import observed_urls
    from hydra.storage import ContentStore

    store = ContentStore(tmp_path / "cas")
    har = {"log": {"entries": [
        {"request": {"url": "https://cdn.other.example/x.js", "method": "GET"}, "response": {"status": 200}},
        {"request": {"url": "https://pko-bonus24.com/oferta/101?utm_source=fb", "method": "GET"},
         "response": {"status": 200}}]}}
    har_d = store.put_bytes(json.dumps(har).encode())
    page = b"<!-- saved from url=(0040)https://www.pko-bonus24.com/oferta/7 -->\n<html></html>"
    page_d = store.put_bytes(page)
    arts = [{"incident_id": "a1", "artifact_id": har_d, "role": "har", "size": 300, "mime": "application/json"},
            {"incident_id": "a1", "artifact_id": page_d, "role": "landing_html", "size": len(page),
             "mime": "text/html"}]
    host = {"host": "pko-bonus24.com", "urls": []}
    urls = observed_urls(store, arts, host)
    assert urls[0].startswith("https://pko-bonus24.com/oferta/101")
    assert "https://www.pko-bonus24.com/oferta/7" in urls and not any("other.example" in u for u in urls)
    assert observed_urls(store, arts[1:], {"host": "elsewhere.example", "urls": []}) == []
    assert observed_urls(store, [], {"host": "h.example", "urls": ["https://h.example/a"]}) == ["https://h.example/a"]
