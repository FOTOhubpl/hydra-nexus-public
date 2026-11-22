"""INF genes from public infrastructure metadata (enrichment replay cache, ADR-007).

Extractor `inf_enrich`: registrar, registration month, domain age at observation, NS / MX, DNS A/AAAA (+/24),
ASN + AS name, certificate issuer, SAN siblings and first certificate month (crt.sh), CERT Polska list hit
(host-level). A lookup without a cached record gives status `unavailable` (unknown, never evidence against).
Every gene points to its EnrichmentRecord via `evidence.enrichment_key`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hydra.contracts import Family, GeneStatus
from hydra.enrich import dns_consensus, enrich_incident, get_enricher
from hydra.enrich.asn import EDGE_ASNS, HOSTER_ASNS
from hydra.enrich.client import record_key
from hydra.enrich.dns import answers
from hydra.enrich.domains import icann_domain, is_ip, registrable_domain
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.har import CDN_IP_PREFIXES, _ip24
from hydra.genome.lure import emit_registrar_window
from hydra.genome.tollfraud import lb_genes
from hydra.genome.webparse import GeneSink

# Shared infrastructure ASNs: evidence of "same CDN/hoster", almost never of "same operator".
CDN_ASNS = {**EDGE_ASNS, **HOSTER_ASNS}  # ASN genes of these networks are boilerplate
SHARED_NS = (
    "cloudflare.com",
    "awsdns",
    "domaincontrol.com",
    "registrar-servers.com",
    "googledomains.com",
    "dns-parking.com",
    "nsone.net",
    "azure-dns",
    "hostinger",
    "namecheaphosting.com",
)
# Default nameservers of registrars and mass hosters (public corpus, PUBLIC_CORPUS.md: ina1-3.registrar.eu served
# 124 unrelated domains). One NS set serves every customer that did not change it: platform, not operator.
PROVIDER_NS = (
    "registrar.eu",
    "openprovider.nl",
    "openprovider.eu",
    "openprovider.com",
    "dyna-ns.net",
    "dnsowl.com",
    "spaceship.net",
    "porkbun.com",
    "name.com",
    "namebrightdns.com",
    "cyberfolks.pl",
    "nazwa.pl",
    "home.pl",
    "ovh.net",
    "hostinger.com",
    "dns.com",
    "share-dns.com",
    "share-dns.net",
    "gname-dns.com",
    "dnspod.net",
    "alidns.com",
    "hichina.com",
    "webhostbox.net",
    "bluehost.com",
    "hostgator.com",
    "dreamhost.com",
    "aftermarket.pl",
    "atthost.pl",
    "lh.pl",
    "zenbox.pl",
    "1dns.pl",
    "domeny.tv",
    "namesilo.com",
    "dynadot.com",
    "registrar-servers.com",
    "domaincontrol.com",
    "dns-parking.com",
    "sedoparking.com",
    "parkingcrew.net",
    "bodis.com",
    "above.com",
    "ui-dns.com",
    "ui-dns.de",
    "ui-dns.org",
    "ui-dns.biz",
    "nsone.net",
    "squarespacedns.com",
    "wixdns.net",
    "websitewelcome.com",
    "your-server.de",
    "linode.com",
    "digitalocean.com",
    "vultr.com",
    "contabo.net",
    "netim.net",
    "gandi.net",
    "cloudns.net",
    "he.net",
    "nic.ru",
    "reg.ru",
    "beget.com",
    "timeweb.ru",
    "ultradns.net",
    "namecheaphosting.com",
)
EDGE_NS = ("cloudflare.com", "awsdns", "azure-dns", "googledomains.com")


def _ns_match(ns: str, suffixes: tuple[str, ...]) -> bool:
    ns = ns.lower().rstrip(".")
    return any((x in ns) if "." not in x else (ns == x or ns.endswith("." + x)) for x in suffixes)


def ns_tier(ns: str) -> str | None:
    """None = operator (own / unusual NS); 'boilerplate' for CDN-DNS hosts; 'platform' for registrar/hoster defaults."""
    if _ns_match(ns, EDGE_NS) or ".awsdns-" in ns:
        return "boilerplate"
    if _ns_match(ns, PROVIDER_NS) or any(x in ns for x in SHARED_NS):
        return "platform"
    return None


def _ns_quality(ns: str) -> float:
    return {"boilerplate": 0.1, "platform": 0.3}.get(ns_tier(ns) or "", 1.0)


def _dt(s: str | None) -> datetime | None:
    """Parse ISO-8601 to an aware UTC datetime. A naive value is taken as UTC (HAR, RDAP, crt.sh are UTC;
    sources in local time, e.g. Windows screenshot names in Europe/Warsaw, must carry an offset upstream)."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def age_bucket(days: float) -> str:
    for lim, name in ((7, "<7d"), (30, "<30d"), (90, "<90d"), (365, "<1y"), (3 * 365, "<3y")):
        if days < lim:
            return name
    return ">=3y"


@register("inf_enrich", family=Family.INF, version=4)  # 2: variants-pipe | enrich-plus; 3: both; 4: + lb_genes
def inf_enrich(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, "inf_enrich@4")
    enr = get_enricher(ctx.enrich, ctx.settings)
    f = enrich_incident(inc, enr)
    if not f.hosts:
        sink.missing("registrar", GeneStatus.NOT_APPLICABLE, "no operator host in the material", "inf_reg")
        return sink.result()
    obs = _dt(f.observed_at)
    # ---- registration (RDAP)
    for d, fact in f.rdap.items():
        r, s = fact.resp, fact.summary
        key = r.key
        if not r.ok:
            sink.add(
                "registrar",
                None,
                "inf_reg",
                status=GeneStatus.UNAVAILABLE,
                enrichment_key=key,
                note=f"rdap {d}: {r.status}",
            )
            continue
        if s.get("registrar"):
            sink.add("registrar", str(s["registrar"])[:120], "inf_reg", enrichment_key=key, note=d, quality=0.3)
        reg = _dt(s.get("registration"))
        if reg:
            sink.add("registration_month", reg.strftime("%Y-%m"), None, enrichment_key=key, note=d, quality=0.4)
            # same registrar + same registration day = batch registration by one operator (lure domains in the
            # sample were registered on one day); the day alone is weaker, so both share one subgroup
            sink.add("registration_day", reg.strftime("%Y-%m-%d"), None, enrichment_key=key, note=d, quality=0.6)
            if s.get("registrar"):
                sink.add("registrar_day", f"{s['registrar']}|{reg:%Y-%m-%d}"[:150], None, enrichment_key=key, note=d)
                # G13: same registrar within +-2 days (3-day buckets on 3 offset grids); large retail = boilerplate
                emit_registrar_window(sink, str(s["registrar"]), reg, domain=d, key=key)
            if obs:
                days = (obs - reg).total_seconds() / 86400
                sink.add(
                    "domain_age_at_observation",
                    age_bucket(days),
                    "inf_reg",
                    enrichment_key=key,
                    note=f"{d}: {days:.0f} days",
                    quality=0.2,
                )
        for ns in s.get("nameservers") or []:
            sink.add("ns_host", ns, ns_tier(ns), enrichment_key=key, note=d, quality=_ns_quality(ns))
        if s.get("nameservers"):
            ns_shared = all(ns_tier(ns) is not None for ns in s["nameservers"])
            # a Cloudflare NS pair is per-account: weak evidence of one CF account (platform tier, low quality)
            sink.add(
                "ns_set",
                ",".join(s["nameservers"]),
                "platform" if ns_shared else None,
                enrichment_key=key,
                note=d,
                quality=0.3 if ns_shared else 0.8,
            )
    # ---- DNS consensus (3 resolvers + random-label wildcard probe, ADOPT #11): decides how host answers count
    wild: dict[str, str] = {}  # host -> zone that answers any label
    gone: set[str] = set()  # not current / resolver-disputed: host-level DNS is unknown, never a penalty
    for host, fact in sorted(f.consensus.items()):
        cls = fact.summary.get("class")
        if cls == dns_consensus.WILDCARD:
            wild[host] = fact.summary.get("wildcard_zone") or fact.summary.get("apex") or host
        elif cls in (dns_consensus.NOT_CURRENT, dns_consensus.DISPUTED):
            gone.add(host)
            sink.add("dns_ip", None, None, status=GeneStatus.UNAVAILABLE, enrichment_key=fact.resp.key,
                     note=f"dns consensus {host}: {cls} ({fact.summary.get('agreement') or 'budget'})")

    def ip_genes(ip: str, name: str, key: str, note: str, group: str | None = None, q: float = 1.0) -> None:
        fa = f.asn.get(ip)
        cdn = ip.startswith(CDN_IP_PREFIXES) or bool(fa and fa.summary.get("asn") in EDGE_ASNS)
        grp = group or ("boilerplate" if cdn else None)
        sink.add("dns_ip", ip, grp, enrichment_key=key, note=note, quality=min(q, 0.1 if cdn else 1.0))
        n24 = _ip24(ip)
        if n24:
            sink.add("dns_ip24", n24, grp, enrichment_key=key, note=note, quality=min(q, 0.06 if cdn else 0.6))

    # ---- DNS (single-resolver answers; A/AAAA/CNAME of wildcard or gone hosts are decided by the consensus)
    for (name, t), r in f.dns.items():
        if not r.ok or (t in ("A", "AAAA", "CNAME") and (name in wild or name in gone)):
            continue
        vals = answers(r)
        if t == "NS":
            for ns in vals:
                ns = ns.lower().rstrip(".")
                sink.add("ns_host", ns, ns_tier(ns), enrichment_key=r.key, note=name, quality=_ns_quality(ns))
        elif t == "MX":
            for mx in vals:
                host = mx.split()[-1].lower().rstrip(".")
                if host:
                    sink.add("mx_host", host, "inf_dns", enrichment_key=r.key, note=name, quality=0.7)
        elif t == "TXT":
            for txt in vals:
                low = txt.lower()
                if "google-site-verification" in low or "facebook-domain-verification" in low or "ms=" in low:
                    sink.add("dns_verification_token", txt.strip('"')[:120], "inf_dns", enrichment_key=r.key, note=name)
        elif t == "CNAME":
            for c in vals:
                sink.add("dns_cname", c.lower().rstrip("."), "inf_dns", enrichment_key=r.key, note=name, quality=0.6)
                lb_genes(sink, c.lower().rstrip("."), enrichment_key=r.key, note=f"CNAME {name}")
        elif t in ("A", "AAAA"):
            if r.status == "NXDOMAIN":
                sink.add("dns_rcode", "NXDOMAIN", None, enrichment_key=r.key, note=name, quality=0.0)
            for ip in vals:
                if is_ip(ip):
                    ip_genes(ip, name, r.key, name)
    # consensus answers of current hosts (fills hosts without a single-resolver record; agreement in the note)
    for host, fact in sorted(f.consensus.items()):
        s = fact.summary
        if s.get("class") != dns_consensus.CURRENT:
            continue
        for ip in s.get("addrs") or []:
            n = (s.get("addr_agreement") or {}).get(ip, 0)
            ip_genes(ip, host, fact.resp.key, f"{host}; {n}/3 resolvers", q=1.0 if n >= 2 else 0.5)
        for c in s.get("cnames") or []:
            sink.add("dns_cname", c, "inf_dns", enrichment_key=fact.resp.key, note=host, quality=0.6)
    # wildcard zones: one platform-tier zone gene; host-level IP/CNAME genes are non-identifying (0 bits); the
    # zone's answer is kept once as a zone-level gene (dedup on value: 50 random subdomains = one observation)
    for host, zone in sorted(wild.items()):
        fact = f.consensus[host]
        key, s = fact.resp.key, fact.summary
        ctl = ((s.get("controls") or {}).get(zone) or ["?"])[0]
        sink.add("dns_wildcard", zone, None, enrichment_key=key, quality=0.3,
                 note=f"zone answers random labels (e.g. {ctl}); seen via {host}")
        tag = f"{host}: wildcard-derived (zone {zone}), not host-specific"
        for ip in s.get("addrs") or []:
            ip_genes(ip, host, key, tag, group="inf:boilerplate:dns_wildcard", q=0.05)
            sink.add("dns_zone_ip", ip, None, enrichment_key=key, note=f"wildcard zone {zone}", quality=0.5)
        for c in s.get("cnames") or []:
            sink.add("dns_cname", c, "inf:boilerplate:dns_wildcard", enrichment_key=key, note=tag, quality=0.05)
            sink.add("dns_zone_cname", c, None, enrichment_key=key, note=f"wildcard zone {zone}", quality=0.5)
    # ---- ASN
    for ip, fact in f.asn.items():
        s = fact.summary
        if not s.get("asn"):
            continue
        shared = s["asn"] in CDN_ASNS
        sink.add(
            "asn",
            f"AS{s['asn']}",
            "boilerplate" if shared else None,
            enrichment_key=record_key("asn", ip),
            note=ip,
            quality=0.1 if shared else 0.8,
        )
        if s.get("as_name"):
            sink.add(
                "as_org",
                str(s["as_name"])[:120],
                "boilerplate" if shared else None,
                enrichment_key=record_key("asn", ip),
                note=ip,
                quality=0.1 if shared else 0.6,
            )
        if s.get("prefix"):
            sink.add(
                "bgp_prefix",
                s["prefix"],
                "boilerplate" if shared else None,
                enrichment_key=record_key("asn", ip),
                note=ip,
                quality=0.05 if shared else 0.7,
            )
    # ---- TLS probe (JARM-like, own stdlib variant; cache only, filled on analyst request by scripts/tls_probe.py)
    for host, fact in sorted(f.tls.items()):
        fp = fact.summary.get("fingerprint")
        if not fact.resp.ok or not fp:
            continue
        ip = fact.summary.get("ip") or ""
        fa = f.asn.get(ip)
        edge = ip.startswith(CDN_IP_PREFIXES) or bool(fa and fa.summary.get("asn") in EDGE_ASNS)
        sink.add("tls_probe_hash", fp, "boilerplate" if edge else None, enrichment_key=fact.resp.key,
                 quality=0.1 if edge else 0.3, note=f"{host} ({ip})" + (" CDN edge" if edge else ""))
    # ---- certificates (crt.sh)
    for d, fact in f.crtsh.items():
        r, s = fact.resp, fact.summary
        if not r.ok:
            sink.add(
                "cert_san_sibling",
                None,
                "inf_cert",
                status=GeneStatus.UNAVAILABLE,
                enrichment_key=r.key,
                note=f"crt.sh {d}: {r.status}",
            )
            continue
        for iss in s.get("issuers") or []:
            sink.add("cert_issuer", iss[:100], "inf_cert", enrichment_key=r.key, note=d, quality=0.1)
        own = {h for h in f.hosts} | {d}
        for san in s.get("sans") or []:
            if san in own or san.endswith("." + d) or not registrable_domain(san):
                continue
            sink.add("cert_san_sibling", san, "inf_cert", enrichment_key=r.key, note=f"on a certificate of {d}")
            sd = icann_domain(san)
            if sd and sd != d:
                sink.add("cert_san_domain", sd, "inf_cert", enrichment_key=r.key, note=f"via {d}")
        fc = _dt(s.get("first_cert"))
        if fc:
            sink.add("cert_first_month", fc.strftime("%Y-%m"), "inf_reg", enrichment_key=r.key, note=d, quality=0.3)
    # ---- CERT Polska warning list (host level; parent hit reported separately)
    for host, s in f.cert_pl.items():
        if s.get("host_listed"):
            sink.add(
                "cert_pl_listed_host",
                host,
                "inf_certpl",
                note=f"listed {s.get('insert_date')} (snapshot {s.get('snapshot')})",
                quality=1.0,
            )
        elif s.get("parent_listed"):
            sink.add(
                "cert_pl_listed_parent",
                s["parent_listed"],
                "inf_certpl",
                quality=0.4,
                note=f"parent of {host} listed {s.get('parent_insert_date')}",
            )
    for d in f.domains:
        if d not in f.rdap and d not in f.crtsh:
            sink.missing("registrar", GeneStatus.UNAVAILABLE, f"no enrichment for {d}", "inf_reg")
    from hydra.genome import parked

    v = parked.incident_verdict(inc, ctx)
    if v is not None:  # parked capture: resolved addresses are the parking provider's
        parked.downgrade_ips(sink.genes, v, ("dns_ip", "dns_ip24", "dns_zone_ip"))
    return sink.result()
