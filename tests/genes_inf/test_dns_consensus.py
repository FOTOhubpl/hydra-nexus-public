"""Wildcard-aware DNS consensus (ADOPT #11): synthetic zones under .test, fake resolvers, replay determinism."""

from __future__ import annotations

import itertools
import json

import pytest
from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.contracts import GeneStatus, Incident
from hydra.enrich import Fact, IncidentFacts
from hydra.enrich import dns_consensus as dc
from hydra.enrich.client import Enricher
from hydra.genome import inf
from hydra.genome.base import ExtractContext, ResolvedIncident
from hydra.synapse import level

R3 = dc.RESOLVERS3


def zone_resolver(records: dict[str, list[str]], failing: dict[str, str] | None = None, calls: list | None = None):
    """records: exact name or '*.zone' -> A addresses ('cname:x' entries become CNAMEs). AAAA is always NODATA."""
    failing = failing or {}

    def resolve(name: str, rtype: str, res: str) -> dict:
        if calls is not None:
            calls.append((name, rtype, res))
        if res in failing:
            return {"rcode": failing[res], "addrs": [], "cnames": []}
        labels = name.split(".")
        hit = records.get(name)
        if hit is None:
            for i in range(1, len(labels)):
                hit = records.get("*." + ".".join(labels[i:]))
                if hit is not None:
                    break
        if hit is None:
            return {"rcode": "NXDOMAIN", "addrs": [], "cnames": []}
        if rtype == "AAAA":
            return {"rcode": "NOERROR", "addrs": [], "cnames": [c[6:] for c in hit if c.startswith("cname:")]}
        return {"rcode": "NOERROR", "addrs": [a for a in hit if not a.startswith("cname:")],
                "cnames": [c[6:] for c in hit if c.startswith("cname:")]}

    return resolve


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    """Fake resolvers only (no `dig`, socket guard on): the live code paths need HYDRA_OFFLINE=0."""
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)


def counter_rng():
    n = itertools.count()
    return lambda: f"{next(n):032x}"


def run(host, records, tmp_path, **kw):
    enr = Enricher(cache_dir=tmp_path / "enrich", live=True)
    return dc.check(enr, host, resolve=zone_resolver(records, **kw), budget=dc.Budget(10_000), rng=counter_rng())


def test_control_depths():
    assert dc.control_depths("a.b.c.example.pl", "example.pl") == ["example.pl", "c.example.pl", "b.c.example.pl"]
    assert dc.control_depths("example.pl", "example.pl") == ["example.pl"]


def test_wildcard_zone_detected(tmp_path):
    rec = {"*.wild.test": ["45.83.100.7"]}
    for h in ("x1.wild.test", "x2.wild.test"):
        _, s = run(h, rec, tmp_path)
        assert s["class"] == dc.WILDCARD and s["wildcard_zone"] == "wild.test" and s["addrs"] == ["45.83.100.7"]


def test_current_host_with_nxdomain_controls(tmp_path):
    _, s = run("www.real.test", {"www.real.test": ["45.83.101.5"]}, tmp_path)
    assert s["class"] == dc.CURRENT and s["agreement"] == "3/3" and s["addr_agreement"] == {"45.83.101.5": 3}


def test_one_servfail_is_current_two_is_disputed(tmp_path):
    rec = {"www.real.test": ["45.83.101.5"]}
    _, s = run("www.real.test", rec, tmp_path / "a", failing={R3[0]: "SERVFAIL"})
    assert s["class"] == dc.CURRENT and s["agreement"] == "2/3"
    _, s = run("www.real.test", rec, tmp_path / "b", failing={R3[0]: "SERVFAIL", R3[1]: "SERVFAIL"})
    assert s["class"] == dc.DISPUTED


def test_not_current_and_filtering_resolver(tmp_path):
    _, s = run("gone.real.test", {}, tmp_path / "a")
    assert s["class"] == dc.NOT_CURRENT
    # one filtering resolver answers a block-page IP for a dead name: two clean negatives win
    def blocky(name, rtype, res):
        if res == R3[2] and rtype == "A":
            return {"rcode": "NOERROR", "addrs": ["45.83.102.66"], "cnames": []}
        return {"rcode": "NXDOMAIN", "addrs": [], "cnames": []}

    enr = Enricher(cache_dir=tmp_path / "b", live=True)
    _, s = dc.check(enr, "gone.real.test", resolve=blocky, budget=dc.Budget(1000), rng=counter_rng())
    assert s["class"] == dc.NOT_CURRENT


def test_deep_wildcard_found_at_closest_depth(tmp_path):
    _, s = run("a.b.example.test", {"*.b.example.test": ["45.83.100.9"]}, tmp_path)
    assert s["class"] == dc.WILDCARD and s["wildcard_zone"] == "b.example.test"


def test_wildcard_cname_to_rotating_cdn(tmp_path):
    """Same CNAME target, different rotated IP sets: still wildcard (documented builder decision)."""
    seq = itertools.count()

    def rot(name, rtype, res):
        if not name.endswith("cdnwild.test") or rtype == "AAAA":
            return {"rcode": "NOERROR", "addrs": [], "cnames": []}
        return {"rcode": "NOERROR", "addrs": [f"45.83.100.{next(seq) % 200}"], "cnames": ["edge.cdn-fixture.test"]}

    enr = Enricher(cache_dir=tmp_path, live=True)
    _, s = dc.check(enr, "promo.cdnwild.test", resolve=rot, budget=dc.Budget(1000), rng=counter_rng())
    assert s["class"] == dc.WILDCARD


def test_budget_exhausted_is_disputed_and_not_cached(tmp_path):
    enr = Enricher(cache_dir=tmp_path, live=True)
    r, s = dc.check(enr, "www.real.test", resolve=zone_resolver({}), budget=dc.Budget(5), rng=counter_rng())
    assert s["class"] == dc.DISPUTED and r.status == "error:budget"
    assert not (tmp_path / "dns_consensus" / "www.real.test.json").exists()


def test_needs_three_resolvers(tmp_path):
    with pytest.raises(ValueError):
        dc.check(Enricher(cache_dir=tmp_path, live=False), "a.test", resolvers=("1.1.1.1", "8.8.8.8"))


def test_replay_is_deterministic_and_offline(tmp_path):
    _, live = run("x1.wild.test", {"*.wild.test": ["45.83.100.7"]}, tmp_path)
    p = tmp_path / "enrich" / "dns_consensus" / "x1.wild.test.json"  # lowercase key: Linux is case-sensitive
    stored = json.loads(p.read_text())
    assert stored["data"]["controls"]["wild.test"][0].startswith("hx")
    calls: list = []
    replay = Enricher(cache_dir=tmp_path / "enrich", live=False)
    r1, s1 = dc.check(replay, "X1.Wild.Test.", resolve=zone_resolver({}, calls=calls))
    r2, s2 = dc.check(replay, "x1.wild.test", resolve=zone_resolver({}, calls=calls))
    assert calls == [] and s1 == s2 == live and r1.key == r2.key
    # no cache, replay only: unknown (None), callers keep the single-resolver answers
    _, s = dc.check(replay, "never.seen.test", resolve=zone_resolver({}, calls=calls))
    assert s == {"class": None} and calls == []


def test_parse_dig_output():
    out = (";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n"
           "promo.x.test.\t60\tIN\tCNAME\tEdge.CDN.test.\nedge.cdn.test.\t60\tIN\tA\t45.83.100.5\n")
    a = dc.parse_dig(out, "A")
    assert a == {"rcode": "NOERROR", "addrs": ["45.83.100.5"], "cnames": ["edge.cdn.test"], "error": None}
    assert dc.parse_dig(";; ->>HEADER<<- status: NOERROR, id: 2\n", "AAAA")["rcode"] == "NODATA"
    assert dc.parse_dig("", "A")["error"] == "noresp"


# ------------------------------------------------------------------------------------------ genes


def _incident(iid: str) -> ResolvedIncident:
    return ResolvedIncident(Incident(id=iid, source="test", source_ref=iid, artifacts=[]), [])


def _genes(monkeypatch, settings, tmp_path, iid, hosts, records):
    enr = Enricher(cache_dir=tmp_path / "enrich", live=True)
    facts = IncidentFacts(hosts=hosts, landing_hosts=hosts[:1], domains=[], observed_at=None)
    for h in hosts:
        r, s = dc.check(enr, h, resolve=zone_resolver(records), budget=dc.Budget(100_000), rng=counter_rng())
        facts.consensus[h] = Fact(r, s)
    monkeypatch.setattr(inf, "enrich_incident", lambda inc, enr: facts)
    ctx = ExtractContext(settings=settings, work_dir=tmp_path, enrich=enr)
    return inf.inf_enrich(_incident(iid), ctx).genes


def test_fifty_wildcard_subdomains_are_one_observation(monkeypatch, settings, tmp_path):
    """ADOPT #11 acceptance: 50 random subdomains of one wildcard zone plus one real host."""
    rec = {"*.wild.test": ["45.83.100.7"], "www.real.test": ["45.83.101.5"]}
    hosts = [f"r{i:02d}x.wild.test" for i in range(50)] + ["www.real.test"]
    genes = _genes(monkeypatch, settings, tmp_path, "inc-w", hosts, rec)
    zone_ip = [g for g in genes if g.kind == "dns_zone_ip"]
    assert [(g.value, g.evidence.note) for g in zone_ip] == [("45.83.100.7", "wildcard zone wild.test")]
    assert level(zone_ip[0]) == "campaign"
    wild = [g for g in genes if g.kind == "dns_wildcard"]
    assert [g.value for g in wild] == ["wild.test"] and level(wild[0]) == "kit"
    ips = {g.value: g for g in genes if g.kind == "dns_ip" and g.value}
    assert level(ips["45.83.100.7"]) == "none" and "wildcard-derived" in ips["45.83.100.7"].evidence.note
    assert level(ips["45.83.101.5"]) == "campaign" and "3/3 resolvers" in ips["45.83.101.5"].evidence.note


def test_subdomain_only_difference_gives_no_host_level_ip_link(monkeypatch, settings, tmp_path):
    rec = {"*.wild.test": ["45.83.100.7"]}
    a = _genes(monkeypatch, settings, tmp_path / "a", "inc-a", ["aa1.wild.test"], rec)
    b = _genes(monkeypatch, settings, tmp_path / "b", "inc-b", ["bb2.wild.test"], rec)
    host_level = lambda gs: {g.value for g in gs if g.kind in ("dns_ip", "dns_ip24") and level(g) != "none"}  # noqa: E731
    assert host_level(a) == host_level(b) == set()
    assert [g.value for g in a if g.kind == "dns_zone_ip"] == [g.value for g in b if g.kind == "dns_zone_ip"]


def test_gone_and_disputed_hosts_are_unavailable(monkeypatch, settings, tmp_path):
    genes = _genes(monkeypatch, settings, tmp_path, "inc-g", ["gone.real.test"], {})
    g = [x for x in genes if x.kind == "dns_ip"]
    assert len(g) == 1 and g[0].status == GeneStatus.UNAVAILABLE and "not_current" in g[0].evidence.note
