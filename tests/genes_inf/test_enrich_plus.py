"""HTTP header fingerprints, TLS probe and the HYDRA_OFFLINE kill switch (synthetic data, no network)."""

from __future__ import annotations

import hashlib
import json

import pytest
from fixtures import _art, _synthetic_warninglists  # noqa: F401

from hydra.config import Settings
from hydra.contracts import Incident
from hydra.enrich import Fact, IncidentFacts, tlsprobe
from hydra.enrich import dns_consensus as dc
from hydra.enrich.client import Enricher
from hydra.genome import http_fp, inf
from hydra.genome.base import ExtractContext, ResolvedIncident, load_family_modules, run_all
from hydra.synapse import level

load_family_modules()

NGINX_ORDER = ["Server", "Date", "Content-Type", "Transfer-Encoding", "Connection", "X-Powered-By", "Set-Cookie",
               "Content-Security-Policy", "Strict-Transport-Security", "X-Frame-Options"]
CSP = "default-src 'self'; script-src 'self' 'nonce-abc123' https://cdn.kit-fixture.test; img-src * data:"


def test_header_order_hash_hhhash_style():
    want = "hhh:1:" + hashlib.sha256(":".join(n.lower() for n in NGINX_ORDER).encode()).hexdigest()
    assert http_fp.header_order_hash(NGINX_ORDER) == want
    h2 = [":status"] + [n.lower() for n in NGINX_ORDER]  # HTTP/2 capture: lower case + pseudo-header
    assert http_fp.header_order_hash(h2) == want
    assert http_fp.header_order_hash(NGINX_ORDER[::-1]) != want
    assert http_fp.header_order_hash(["Date", "Server"]) is None


def test_security_profile_and_csp_normalisation():
    assert http_fp.security_profile(NGINX_ORDER) == "csp,hsts,xfo"
    assert http_fp.security_profile(["Date"]) == "none"
    a = http_fp.csp_normalised(CSP)
    b = http_fp.csp_normalised("img-src data: *; script-src https://cdn.kit-fixture.test 'nonce-zzz' 'self';"
                               "default-src 'self'")
    assert a == b and "'nonce'" in a


def _har(host: str, order: list[str], ip: str = "45.83.100.20") -> bytes:
    vals = {"Content-Security-Policy": CSP, "Set-Cookie": "sid=SECRETVALUE123; HttpOnly"}
    entry = {"startedDateTime": "2026-10-09T08:00:00.000Z", "time": 5, "_resourceType": "document",
             "serverIPAddress": ip, "request": {"method": "GET", "url": f"https://{host}/", "headers": []},
             "response": {"status": 200, "redirectURL": "",
                          "headers": [{"name": n, "value": vals.get(n, "x")} for n in order],
                          "content": {"mimeType": "text/html", "size": 30, "text": "<html><p>Oferta</p></html>"}}}
    return json.dumps({"log": {"creator": {"name": "fixture"}, "pages": [], "entries": [entry]}}).encode()


def test_inf_http_genes(settings, tmp_path):
    art = _art(tmp_path, "c.har", _har("www.lure-fixture.live", NGINX_ORDER), "har", "application/json")
    inc = ResolvedIncident(Incident(id="h-1", source="t", source_ref="h-1", artifacts=[art.meta]), [art])
    genes = run_all(inc, ExtractContext(settings=settings, work_dir=tmp_path), only=["inf_http"]).genes
    by = {g.kind: g for g in genes}
    assert by["http_header_order_hash"].value.startswith("hhh:1:") and level(by["http_header_order_hash"]) == "kit"
    assert by["http_security_headers"].value == "csp,hsts,xfo" and level(by["http_security_headers"]) == "none"
    assert level(by["http_csp_hash"]) == "kit" and "cdn.kit-fixture.test" in by["http_csp_hash"].evidence.note
    assert "SECRETVALUE123" not in json.dumps([g.model_dump() for g in genes])
    empty = ResolvedIncident(Incident(id="h-2", source="t", source_ref="h-2", artifacts=[]), [])
    g2 = run_all(empty, ExtractContext(settings=settings, work_dir=tmp_path), only=["inf_http"]).genes
    assert [g.status for g in g2] == ["unavailable"]


# ------------------------------------------------------------------------------------------ TLS probe


def fake_shake(calls: list):
    def shake(ip, sni, spec):
        calls.append((ip, sni, spec[0]))
        name = spec[0]
        if name == "t12_cbc":
            return "fail:handshake_failure"
        if name.startswith("t13"):
            return f"TLSv1.3|TLS_AES_128_GCM_SHA256|{'h2' if 'h2' in name else '-'}"
        return "TLSv1.2|ECDHE-RSA-AES128-GCM-SHA256|" + ("h2" if name == "t12_fwd" else "http/1.1")

    return shake


def test_tls_probe_live_then_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)
    calls: list = []
    live = Enricher(cache_dir=tmp_path, live=True)
    pub = lambda h, p: "45.83.100.20"  # noqa: E731
    r, s = tlsprobe.probe(live, "Landing.Lure-Fixture.Live", shake=fake_shake(calls), resolve=pub)
    assert r.status == "ok" and s["fingerprint"].startswith("tlsp1:") and len(calls) == len(tlsprobe.PROBES)
    assert (tmp_path / "tls_probe" / "landing.lure-fixture.live_443.json").is_file()  # lowercase key
    replay = Enricher(cache_dir=tmp_path, live=False)
    r2, s2 = tlsprobe.cached(replay, "landing.lure-fixture.live")
    assert s2 == s and len(calls) == len(tlsprobe.PROBES)


def test_tls_probe_refuses_non_public_and_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)
    calls: list = []
    enr = Enricher(cache_dir=tmp_path, live=True)
    r, s = tlsprobe.probe(enr, "intranet.test", shake=fake_shake(calls), resolve=lambda h, p: None)
    assert r.status == "error:no_public_ip" and calls == [] and s["fingerprint"] is None
    assert not (tmp_path / "tls_probe" / "intranet.test_443.json").exists()
    assert tlsprobe._public_ip("localhost", 443) is None


def test_tls_gene_from_cache(monkeypatch, settings, tmp_path):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)
    enr = Enricher(cache_dir=tmp_path, live=True)
    r, s = tlsprobe.probe(enr, "landing.lure-fixture.live", shake=fake_shake([]), resolve=lambda h, p: "45.83.100.20")
    facts = IncidentFacts(hosts=["landing.lure-fixture.live"], landing_hosts=["landing.lure-fixture.live"],
                          domains=[], observed_at=None)
    facts.tls["landing.lure-fixture.live"] = Fact(r, s)
    monkeypatch.setattr(inf, "enrich_incident", lambda inc, enr: facts)
    inc = ResolvedIncident(Incident(id="t-9", source="t", source_ref="t-9", artifacts=[]), [])
    genes = inf.inf_enrich(inc, ExtractContext(settings=settings, work_dir=tmp_path, enrich=enr)).genes
    g = [x for x in genes if x.kind == "tls_probe_hash"]
    assert len(g) == 1 and g[0].value == s["fingerprint"] and level(g[0]) == "kit" and g[0].evidence.enrichment_key


# ------------------------------------------------------------------------------------------ kill switch


KILL = [{"HYDRA_OFFLINE": "1", "HYDRA_ENRICH": "1"}, {"HYDRA_OFFLINE": "0", "HYDRA_ENRICH": "0"}]


@pytest.mark.parametrize("env", KILL)
def test_kill_switch_blocks_every_live_path(monkeypatch, tmp_path, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Settings(cache_dir=tmp_path / "cache", state_dir=tmp_path / "state")
    enr = Enricher(settings=s)
    assert enr.live is False
    calls: list = []
    r, summ = dc.check(enr, "www.real.test", resolve=lambda *a: calls.append(a) or {"rcode": "NXDOMAIN"})
    assert summ == {"class": None} and r.status == "unavailable"
    r, summ = tlsprobe.probe(enr, "www.real.test", shake=fake_shake(calls), resolve=lambda h, p: "45.83.100.20")
    assert r.status == "unavailable" and summ["fingerprint"] is None
    assert calls == [] and not any((tmp_path / "cache").rglob("*.json"))
