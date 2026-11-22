"""Client request building and response parsing against hand-written fixtures (no network)."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from hydra.db import Writer
from hydra.enrich.client import Enricher
from hydra.integrations import clients, secrets, state
from hydra.integrations.clients import abuseipdb, local, openphish, safebrowsing, urlhaus, urlscan, virustotal
from integrations.conftest import FIXTURES, SECRET, fixture


@pytest.mark.parametrize("value,kind", [("example.com", "domain"), ("Sub.Example.COM.", "domain"),
                                        ("https://a.example/x?y=1", "url"), ("203.0.113.10", "ip"),
                                        ("2001:db8::1", "ip"), ("a" * 64, "sha256"), ("not a host", None),
                                        ("javascript:alert(1)", None), ("", None), ("localhost", None)])
def test_kind_of(value, kind):
    assert clients.kind_of(value) == kind


def test_virustotal():
    keys = {"VT_API_KEY": SECRET}
    r = virustotal.build("energia-zysk.example", "domain", keys)
    assert r.url.endswith("/api/v3/domains/energia-zysk.example") and r.headers == {"x-apikey": SECRET}
    u = virustotal.build("http://a.example/", "url", keys)
    assert u.url.endswith("/urls/" + virustotal.url_id("http://a.example/")) and "=" not in u.url.rsplit("/", 1)[1]
    assert virustotal.build("a" * 64, "sha256", keys).url.endswith("/files/" + "a" * 64)
    v, d = virustotal.parse("x", "domain", "200", fixture("virustotal", "domain_malicious.json"))
    assert v == "malicious" and d["malicious"] == 7
    assert virustotal.parse("x", "domain", "200", fixture("virustotal", "domain_clean.json"))[0] == "clean"
    assert virustotal.parse("x", "domain", "404", {})[0] == "unknown"


def test_urlscan():
    r = urlscan.build("energia-zysk.example", "domain", {"URLSCAN_API_KEY": SECRET})
    assert "q=domain%3Aenergia-zysk.example" in r.url and r.headers["API-Key"] == SECRET
    v, d = urlscan.parse("energia-zysk.example", "domain", "200", fixture("urlscan", "search.json"))
    assert v == "malicious" and d["returned"] == 2 and d["ips"] == ["203.0.113.10"]
    assert d["first_seen"].startswith("2026-10-02")
    assert urlscan.parse("x", "domain", "200", {"results": [], "total": 0})[0] == "unknown"


def test_urlhaus():
    r = urlhaus.build("energia-zysk.example", "domain", {"URLHAUS_AUTH_KEY": SECRET})
    assert r.method == "POST" and r.url.endswith("/v1/host/") and r.headers["Auth-Key"] == SECRET
    assert urllib.parse.parse_qs(r.body.decode()) == {"host": ["energia-zysk.example"]}
    assert urlhaus.build("http://a.example/x", "url", {}).url.endswith("/v1/url/")
    v, d = urlhaus.parse("x", "domain", "200", fixture("urlhaus", "host_ok.json"))
    assert v == "malicious" and d["online"] == 1 and d["threats"] == ["malware_download"]
    assert urlhaus.parse("x", "domain", "200", fixture("urlhaus", "no_results.json"))[0] == "unknown"


def test_abuseipdb():
    r = abuseipdb.build("203.0.113.10", "ip", {"ABUSEIPDB_KEY": SECRET})
    assert "ipAddress=203.0.113.10" in r.url and r.headers == {"Key": SECRET}
    v, d = abuseipdb.parse("203.0.113.10", "ip", "200", fixture("abuseipdb", "check.json"))
    assert v == "malicious" and d["total_reports"] == 41


def test_safebrowsing():
    r = safebrowsing.build("energia-zysk.example", "domain", {"GOOGLE_SAFE_BROWSING_KEY": SECRET})
    body = json.loads(r.body)
    assert body["threatInfo"]["threatEntries"] == [{"url": "http://energia-zysk.example/"}]
    assert r.url.endswith("key=" + SECRET)
    assert safebrowsing.parse("x", "url", "200", fixture("google-safe-browsing", "matches.json"))[0] == "malicious"
    assert safebrowsing.parse("x", "url", "200", {})[0] == "unknown"


def test_openphish_local_match(online_settings):
    urls, hosts = openphish.parse_feed(fixture("openphish", "feed.json")["feed"])
    assert len(urls) == 2
    m = {"fetched_at": "2026-11-22T00:00:00+00:00"}
    assert openphish.match("https://stawy-zdrowie.example/login", "url", urls, hosts, m)["verdict"] == "malicious"
    assert openphish.match("stawy-zdrowie.example", "domain", urls, hosts, m)["verdict"] == "suspicious"
    assert openphish.match("clean.example", "domain", urls, hosts, m)["verdict"] == "unknown"
    assert openphish.match("1.2.3.4", "ip", urls, hosts, m) is None


def test_openphish_refresh_sends_nothing(online_settings, transport, public_dns):
    transport.add("openphish", fixture("openphish", "feed.json")["feed"].encode())
    m = openphish.refresh(online_settings, None, require_enabled=False)
    assert m["entries"] == 2 and m["refreshed"]
    assert len(transport.calls) == 1 and transport.calls[0]["method"] == "GET" and transport.calls[0]["body"] is None
    assert openphish.refresh(online_settings, None, require_enabled=False)["refreshed"] is False  # 1 h cache
    assert len(transport.calls) == 1


def test_lookup_caches_and_replays_without_secret(online_settings, offline_settings, transport, public_dns):
    secrets.set_secret("virustotal", "VT_API_KEY", SECRET)
    state.set_enabled(online_settings, "virustotal", True)
    transport.add("virustotal.com", fixture("virustotal", "domain_malicious.json"))
    w = Writer(online_settings.db_path)
    try:
        r = clients.lookup("virustotal", "Energia-Zysk.example", settings=online_settings, writer=w)
    finally:
        w.close()
    assert r["verdict"] == "malicious" and r["indicator"] == "energia-zysk.example" and r["cached"] is False
    files = list((online_settings.enrich_cache / "integ-virustotal").glob("*.json"))
    assert len(files) == 1 and SECRET not in files[0].read_text() and SECRET not in files[0].name
    # offline replay: same cache, no transport call
    n = len(transport.calls)
    enr = Enricher(cache_dir=online_settings.enrich_cache, settings=offline_settings)
    r2 = clients.lookup("virustotal", "energia-zysk.example", settings=offline_settings, writer=None, enricher=enr)
    assert r2["verdict"] == "malicious" and r2["cached"] is True and len(transport.calls) == n


def test_cert_pl_local(monkeypatch, online_settings):
    from hydra.enrich import cert_pl

    monkeypatch.setenv("HYDRA_CERT_PL_DIR", str(FIXTURES / "cert-pl"))
    cert_pl._load.cache_clear()
    try:
        assert local.test("cert-pl", online_settings)[0] is True
        v = local.lookup("cert-pl", "zdrowe-stawy.example", settings=online_settings)
        assert v["verdict"] == "malicious"
        assert local.lookup("cert-pl", "other.example", settings=online_settings)["verdict"] == "unknown"
    finally:
        cert_pl._load.cache_clear()


def test_push_requests(monkeypatch, online_settings):
    from hydra.integrations import exports
    from hydra.integrations.clients import push

    secrets.set_secret("splunk-hec", "SPLUNK_HEC_URL", "https://splunk.example.org:8088")
    secrets.set_secret("splunk-hec", "SPLUNK_HEC_TOKEN", SECRET)
    p = exports.Payload("siem_json", "application/json", b"{}", {}, [], [])
    req, vals = push.request_for("splunk-hec", p)
    assert req.url == "https://splunk.example.org:8088/services/collector/event"
    assert req.headers["Authorization"] == f"Splunk {SECRET}" and SECRET in vals
    secrets.set_secret("elasticsearch", "ES_URL", "https://es.example.org/")
    secrets.set_secret("elasticsearch", "ES_API_KEY", SECRET)
    req, _ = push.request_for("elasticsearch", p)
    assert req.url == "https://es.example.org/_bulk" and req.headers["Authorization"] == f"ApiKey {SECRET}"
    assert req.headers["Content-Type"] == "application/x-ndjson"
    secrets.set_secret("misp", "MISP_URL", "https://misp.example.org")
    secrets.set_secret("misp", "MISP_KEY", SECRET)
    req, _ = push.request_for("misp", p)
    assert req.url.endswith("/events/add") and req.headers["Authorization"] == SECRET
