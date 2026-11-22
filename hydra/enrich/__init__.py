"""Online enrichment layer with a replayable evidence cache (ADR-007).

    from hydra.enrich import Enricher, records_for, enrich_incident
    records = records_for(resolved_incident)          # list[EnrichmentRecord] for persistence
    facts = enrich_incident(resolved_incident)        # parsed facts used by the inf_enrich genes

Default: replay-only from `Settings.enrich_cache` (HYDRA_ENRICH_CACHE, default `<cache_dir>/enrich`).
`HYDRA_ENRICH=1` enables
polite live lookups (RDAP, DNS via dig, a three-resolver DNS consensus with wildcard probes, Team Cymru ASN, crt.sh).
The CERT Polska list is an offline snapshot. TLS probe results (`tlsprobe`) are read from the cache only.
Queried: only hostnames and IPs extracted from the material. Never the scam pages themselves.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hydra.config import Settings
from hydra.contracts import EnrichmentRecord, GeneStatus
from hydra.enrich import asn, cert_pl, crtsh, dns, dns_consensus, rdap, tlsprobe
from hydra.enrich.client import Enricher, Response, now_iso, record_key
from hydra.enrich.domains import icann_domain, is_ip, is_platform

if TYPE_CHECKING:
    from hydra.genome.base import ResolvedIncident

__all__ = ["Enricher", "IncidentFacts", "enrich_incident", "records_for", "get_enricher"]

MAX_HOSTS = 40
MAX_IPS = 40

_DEFAULT: Enricher | None = None


def get_enricher(ctx_enrich: Any = None, settings: Settings | None = None) -> Enricher:
    """ctx.enrich if it is an Enricher, else one process-wide Enricher built from Settings."""
    global _DEFAULT
    if isinstance(ctx_enrich, Enricher):
        return ctx_enrich
    want = Enricher(settings=settings)
    if _DEFAULT is None or (_DEFAULT.cache_dir, _DEFAULT.live) != (want.cache_dir, want.live):
        _DEFAULT = want
    return _DEFAULT


@dataclass
class Fact:
    resp: Response
    summary: dict[str, Any]


@dataclass
class IncidentFacts:
    hosts: list[str]
    landing_hosts: list[str]
    domains: list[str]
    observed_at: str | None
    rdap: dict[str, Fact] = field(default_factory=dict)  # domain ->
    crtsh: dict[str, Fact] = field(default_factory=dict)  # domain ->
    dns: dict[tuple[str, str], Response] = field(default_factory=dict)  # (name, type) ->
    ips: dict[str, list[str]] = field(default_factory=dict)  # host -> ips (HAR + DNS)
    asn: dict[str, Fact] = field(default_factory=dict)  # ip ->
    cert_pl: dict[str, dict[str, Any]] = field(default_factory=dict)  # host ->
    consensus: dict[str, Fact] = field(default_factory=dict)  # host -> 3-resolver + wildcard check (ADOPT #11)
    tls: dict[str, Fact] = field(default_factory=dict)  # landing host -> cached TLS probe (replay only)

    def records(self) -> list[EnrichmentRecord]:
        out: dict[str, EnrichmentRecord] = {}
        for f in list(self.rdap.values()) + list(self.crtsh.values()):
            out.setdefault(f.resp.key, f.resp.record(f.summary))
        for ip, f in self.asn.items():  # Team Cymru answers are DNS TXT records; expose them as source "asn"
            rec = f.resp.record({"ip": ip, **f.summary})
            rec.key, rec.source, rec.query = record_key("asn", ip), "asn", ip
            out.setdefault(rec.key, rec)
        for r in self.dns.values():
            out.setdefault(r.key, r.record({"answers": dns.answers(r)}))
        for f in self.tls.values():
            if f.resp.ok:
                out.setdefault(f.resp.key, f.resp.record({"fingerprint": f.summary.get("fingerprint")}))
        for f in self.consensus.values():
            if f.resp.ok or f.resp.status == "error:budget":
                keep = ("class", "apex", "wildcard_zone", "addrs", "cnames", "agreement", "budget_exhausted")
                out.setdefault(f.resp.key, f.resp.record({k: f.summary.get(k) for k in keep}))
        for host, s in self.cert_pl.items():
            k = record_key("cert_pl_list", host)
            out.setdefault(
                k,
                EnrichmentRecord(
                    key=k,
                    source="cert_pl_list",
                    query=host,
                    fetched_at=s.get("retrieved_at") or now_iso(),
                    status=GeneStatus.OBSERVED if cert_pl.available() else GeneStatus.UNAVAILABLE,
                    cache_path=str(cert_pl.snapshot_dir() / "domains.json"),
                    response_sha256=cert_pl.snapshot_meta().get("sha256"),
                    summary=s,
                ),
            )
        return list(out.values())


def _observed_at(inc: ResolvedIncident, b) -> str | None:
    if inc.incident.observed_at:
        return inc.incident.observed_at
    ts = sorted(h.first_ts for h in b.hars if h.first_ts and not h.local_replay)
    return ts[0] if ts else None


def enrich_incident(inc: ResolvedIncident, enr: Enricher | None = None) -> IncidentFacts:
    from urllib.parse import urlsplit

    from hydra.genome.webparse import bundle, first_party_hosts, operator_hosts

    enr = enr or get_enricher()
    b = bundle(inc)
    landing = sorted({(urlsplit(u).hostname or "").lower() for u, _, _ in b.landing_urls} - {""})
    hosts = sorted(first_party_hosts(b) | operator_hosts(b))[:MAX_HOSTS]
    landing = [h for h in landing if h in hosts]
    domains = sorted({d for h in hosts if not is_platform(h) and (d := icann_domain(h))})
    f = IncidentFacts(hosts=hosts, landing_hosts=landing, domains=domains, observed_at=_observed_at(inc, b))
    har_ips: dict[str, set[str]] = {}
    for h in b.hars:
        if h.local_replay:
            continue
        for host, hi in h.hosts.items():
            if host in hosts:
                har_ips.setdefault(host, set()).update(hi.ips)
    for d in domains:
        r, s = rdap.domain(enr, d)
        f.rdap[d] = Fact(r, s)
        r, s = crtsh.lookup(enr, d)
        f.crtsh[d] = Fact(r, s)
        for t in ("NS", "MX", "TXT"):
            f.dns[(d, t)] = dns.query(enr, d, t)
    for host in hosts:
        for t in ("A", "AAAA", "CNAME"):
            f.dns[(host, t)] = dns.query(enr, host, t)
        live = dns.answers(f.dns[(host, "A")]) + dns.answers(f.dns[(host, "AAAA")])
        r, s = dns_consensus.check(enr, host)
        f.consensus[host] = Fact(r, s)
        if s.get("class") in (dns_consensus.CURRENT, dns_consensus.WILDCARD):
            live += list(s.get("addrs") or [])
        f.ips[host] = sorted(har_ips.get(host, set()) | {x for x in live if is_ip(x)})
        f.cert_pl[host] = cert_pl.lookup(host)
        if host in landing:  # active probe results come only from the cache (scripts/tls_probe.py fills it)
            r, s = tlsprobe.cached(enr, host)
            f.tls[host] = Fact(r, s)
    seen: list[str] = []
    for ips in f.ips.values():
        for ip in ips:
            if ip not in seen and _public(ip):
                seen.append(ip)
    for ip in seen[:MAX_IPS]:
        r, s = asn.lookup(enr, ip)
        f.asn[ip] = Fact(r, s)
    return f


def _public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def records_for(inc: ResolvedIncident, enr: Enricher | None = None) -> list[EnrichmentRecord]:
    """Every enrichment response used for this incident, as EnrichmentRecord rows (pipeline persists them)."""
    return enrich_incident(inc, enr).records()
