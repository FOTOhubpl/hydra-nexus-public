"""Polish scam domain-naming genes and public warning-list labels (INF family, PL-SCAM stream 2026-11-21).

Extractor `inf_domain_pl` reads the hosts of an incident (landing URLs, first-party and operator hosts of the saved
page / HAR, host names written in the ad or page text) and emits:

- `pl_domain_brand_lookalike` (brand id, `INF:kit:pl_domain_brand`): a brand token (pl_brands.json) in a host
  whose registrable domain is not the brand's own;
- `pl_domain_template` (`brand+service+shape+tld`, `INF:kit:pl_domain_template`): naming template of a kit, e.g.
  `allegro+offer+word_longdigits+cfd`;
- `pl_domain_shape` (`INF:label:pl_domain_shape`): brand_subdomain_foreign, pl_prefix, word_longdigits,
  brand_service, brand_typo, embedded_tld, random_letters;
- `pl_domain_service_word` (`INF:label:pl_domain_lexeme`): offer, listing, delivery, payment, order, verify,
  refund, ticket, invest;
- `pl_domain_tld_risk` (TLD, `INF:label:pl_domain_tld`): TLD over-represented on the CERT Polska list vs Tranco
  (log2 ratio >= 3);
- `knf_warning_listed` (domain, `INF:label:public_warning`): named in the KNF public warning list;
- `mf_hazard_listed` (host, `INF:label:public_warning`): in the MF register of illegal gambling domains.

Kit-tier genes can support template / campaign-group links but never a same-campaign link (synapse tiers); label
genes are attributes shown with the evidence and count 0 bits. Absence of a pattern is not evidence of anything.
Statistics and word lists: `lexicons/pl_domain_patterns.json`, `lexicons/pl_brands.json` (public sources only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from hydra.contracts import EvidencePointer, Family, Gene, Provenance
from hydra.enrich import public_warnings
from hydra.enrich.domains import is_boilerplate, is_ip, registrable_domain, valid_host
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.lexicons import public

VERSION = 2  # 2: random_letters shape (SCAMLINKS)
EX = f"inf_domain_pl@{VERSION}"
MIN_TOKEN = 3


@dataclass(frozen=True)
class Lexicon:
    brand_tokens: tuple[tuple[str, str], ...]  # (token, brand id), longest token first
    official: dict[str, frozenset[str]]  # brand id -> its registrable domains
    typo_tokens: frozenset[str]
    service: tuple[tuple[str, str], ...]  # (word, class), longest first
    tld: dict[str, dict]
    tld_min: float


@lru_cache(maxsize=2)
def lexicon() -> Lexicon:
    br = public.brands()
    dp = public.load("pl_domain_patterns")
    toks: list[tuple[str, str]] = []
    official: dict[str, frozenset[str]] = {}
    for bid, b in br.items():
        official[bid] = frozenset(d.lower() for d in b.get("official_domains") or [])
        for t in b.get("domain_tokens") or []:
            if len(t) >= MIN_TOKEN:
                toks.append((t.lower(), bid))
    typos = frozenset({"allegrolokalne", "allegrolokainie", "alebilet", "allebilet", "albilet", "inpolst"})
    svc = [(w, c) for c, ws in (dp.get("service_words") or {}).items() for w in ws]
    typos |= {w for w, _ in svc if w in ("ofeta", "ofera", "ofertaa", "ogloszezenie", "ogioszenie", "ogoszenie",
                                         "podukty", "paczzka", "ploferta")}
    return Lexicon(tuple(sorted(set(toks), key=lambda x: -len(x[0]))), official, typos,
                   tuple(sorted(set(svc), key=lambda x: -len(x[0]))), dict(dp.get("tld") or {}),
                   float(dp.get("tld_risky_min_log2", 3.0)))


@dataclass
class DomainFacts:
    host: str
    registrable: str
    brands: list[tuple[str, bool]]  # (brand id, token sits in the registrable label)
    services: list[str]
    shapes: list[str]
    tld: str
    tld_stats: dict | None


def _label_of(host: str, rd: str) -> str:
    return rd[: -(len(rd.split(".", 1)[1]) + 1)] if "." in rd else rd


_CONS_RUN = re.compile(r"[b-df-hj-np-tv-xz]{4,}")
_DIGRAPH = re.compile(r"sz|cz|rz|ch|dz|th|ph|sh|ck|gh|ng|st")


def random_letters(label: str) -> bool:
    """Consonant-heavy short letter-only label ('xkqtrbwe'): the throw-away name shape of mass-registered shop
    domains (SCAMLINKS 2026-11-21). A 0-bit label: many unrelated operators register such names (assumed)."""
    if not re.fullmatch(r"[a-z]{6,14}", label):
        return False
    # Polish / English digraphs count as one consonant: 'sprzedam', 'szczecin', 'strength' are words, not noise
    folded = _DIGRAPH.sub("0", label)
    return bool(_CONS_RUN.search(folded.replace("0", "b")))


def analyse_host(host: str) -> DomainFacts | None:
    """Brand tokens, service words and naming shapes of one host (pure function, no I/O)."""
    host = host.lower().strip(".")
    if not host or is_ip(host) or not valid_host(host):
        return None
    rd = (registrable_domain(host) or host).lower()
    lx = lexicon()
    label = _label_of(host, rd)
    sub = host[: -len(rd)].rstrip(".") if host.endswith(rd) and host != rd else ""
    tld = host.rsplit(".", 1)[-1]
    brands: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for tok, bid in lx.brand_tokens:
        if bid in seen or rd in lx.official.get(bid, frozenset()):
            continue
        in_label, in_sub = tok in label, tok in sub
        if in_label or in_sub:
            if len(tok) <= 4 and not re.search(rf"(^|[^a-z]){re.escape(tok)}([^a-z]|$)", label + "." + sub):
                continue  # short tokens ("pko", "dpd", "olx" stays) must stand alone: "spkoxx" is not PKO
            brands.append((bid, in_label))
            seen.add(bid)
    services: list[str] = []
    for w, cls in lx.service:
        if cls not in services and w in label:
            services.append(cls)
    shapes: list[str] = []
    if any(not in_label for _, in_label in brands) and not any(in_label for _, in_label in brands):
        shapes.append("brand_subdomain_foreign")
    pl_word = label.startswith("pl") and any(label[2:].startswith(w) for w, _ in lx.service)
    if label.startswith("pl-") or pl_word or label.endswith("-pl"):
        shapes.append("pl_prefix")
    if re.search(r"[a-z]-?\d{4,}$", label):
        shapes.append("word_longdigits")
    if any(in_label for _, in_label in brands) and services:
        shapes.append("brand_service")
    if any(t in host for t in lx.typo_tokens):
        shapes.append("brand_typo")
    if re.search(r"(^|[.-])(www|com|pl)-[a-z]", host) or re.search(r"\.(pl|com)-[a-z0-9]", host):
        shapes.append("embedded_tld")
    if not brands and not services and random_letters(label):
        shapes.append("random_letters")
    st = lx.tld.get(tld)
    return DomainFacts(host, rd, brands, services, shapes, tld, st)


def _hosts(inc: ResolvedIncident, ctx: ExtractContext) -> dict[str, tuple[str, str | None]]:
    """host -> (where it was seen, artifact id)."""
    from hydra.genome.webparse import bundle, first_party_hosts, operator_hosts

    out: dict[str, tuple[str, str | None]] = {}
    try:
        b = bundle(inc)
    except Exception:  # noqa: BLE001 — a broken capture is one less source of hosts
        b = None
    if b is not None:
        for u, src, aid in b.landing_urls:
            h = (urlsplit(u).hostname or "").lower()
            if h:
                out.setdefault(h, (f"landing ({src})", aid))
        for h in sorted(first_party_hosts(b) | operator_hosts(b)):
            out.setdefault(h.lower(), ("first-party / operator host", None))
    try:
        from hydra.genome.txt_entities import find_hosts
        from hydra.genome.txt_sources import collect

        for blk in collect(inc, ctx).blocks:
            for s in blk.segments:
                for e in find_hosts(s.text or ""):
                    out.setdefault(e.value.lower(), (f"text ({blk.source})", s.evidence.artifact_id))
    except Exception:  # noqa: BLE001
        pass
    return {h: v for h, v in out.items() if h and not is_boilerplate(h)}


def _gene(inc_id: str, kind: str, value: str, group: str, quality: float, note: str,
          artifact_id: str | None = None) -> Gene:
    return Gene(incident_id=inc_id, family=Family.INF, kind=kind, value=value[:200], quality=round(quality, 3),
                provenance=Provenance.INF, extractor=EX, dependency_group=group,
                evidence=EvidencePointer(artifact_id=artifact_id, note=note[:300]))


def domain_genes(inc_id: str, hosts: dict[str, tuple[str, str | None]]) -> list[Gene]:
    genes: list[Gene] = []
    seen: set[tuple[str, str]] = set()

    def add(kind, value, group, q, note, aid=None):
        if (kind, value) in seen:
            return
        seen.add((kind, value))
        genes.append(_gene(inc_id, kind, value, group, q, note, aid))

    lx = lexicon()
    for host, (where, aid) in sorted(hosts.items()):
        f = analyse_host(host)
        if f is None:
            continue
        for bid, in_label in f.brands:
            add("pl_domain_brand_lookalike", bid, "INF:kit:pl_domain_brand", 0.8 if in_label else 0.7,
                f"{host}: brand token of {bid} {'in the registrable label' if in_label else 'in a subdomain'} of "
                f"{f.registrable}, not an official {bid} domain; seen in {where}", aid)
        for cls in f.services:
            add("pl_domain_service_word", cls, "INF:label:pl_domain_lexeme", 0.5, f"{host}: service word ({cls})", aid)
        for sh in f.shapes:
            add("pl_domain_shape", sh, "INF:label:pl_domain_shape", 0.6, f"{host}: naming shape {sh}", aid)
        risky = f.tld_stats is not None and f.tld_stats["log2_ratio"] >= lx.tld_min
        if risky:
            s = f.tld_stats
            add("pl_domain_tld_risk", f.tld, "INF:label:pl_domain_tld", min(1.0, s["log2_ratio"] / 8.0),
                f"{host}: .{f.tld} has {s['cert_n']} hosts on the CERT Polska list vs {s['tranco_n']} in Tranco top-1M "
                f"(log2 ratio {s['log2_ratio']}, snapshot 2026-11-20); a TLD hint, not a probability", aid)
        brand = "+".join(sorted({b for b, _ in f.brands}))
        if brand and (f.services or f.shapes):
            tmpl = "+".join(x for x in (brand, "/".join(f.services) or "-",
                                        "/".join(s for s in f.shapes if s != "brand_service") or "-", f.tld))
            add("pl_domain_template", tmpl, "INF:kit:pl_domain_template", 0.7,
                f"{host}: naming template brand+service+shape+tld", aid)
    return genes


def warning_genes(inc_id: str, hosts: dict[str, tuple[str, str | None]]) -> list[Gene]:
    genes: list[Gene] = []
    seen: set[tuple[str, str]] = set()
    for host, (_where, aid) in sorted(hosts.items()):
        hit = public_warnings.lookup(host)
        for k in hit.get("knf") or []:
            key = ("knf_warning_listed", k["domain"])
            if key not in seen:
                seen.add(key)
                genes.append(_gene(inc_id, "knf_warning_listed", k["domain"], "INF:label:public_warning",
                                   1.0 if k["level"] == "host" else 0.7,
                                   f"KNF Lista ostrzeżeń publicznych, {k['category'][:90]} (Lp. {k.get('lp')}), "
                                   f"match {k['level']} {host}; snapshot {hit.get('knf_snapshot')}", aid))
        m = hit.get("mf_hazard")
        if m:
            key = ("mf_hazard_listed", m["domain"])
            if key not in seen:
                seen.add(key)
                genes.append(_gene(inc_id, "mf_hazard_listed", m["domain"], "INF:label:public_warning",
                                   1.0 if m["level"] == "host" else 0.7,
                                   f"MF Rejestr domen (nielegalny hazard), wpis {m.get('date_added')} "
                                   f"Lp. {m.get('lp')}, "
                                   f"match {m['level']} {host}; snapshot {hit.get('mf_snapshot')}", aid))
    return genes


@register("inf_domain_pl", family=Family.INF, version=VERSION)
def inf_domain_pl(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    if not public.enabled():
        return ExtractResult()
    hosts = _hosts(inc, ctx)
    if not hosts:
        return ExtractResult()
    iid = inc.incident.id
    return ExtractResult(genes=domain_genes(iid, hosts) + warning_genes(iid, hosts))
