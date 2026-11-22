"""Content-based parked / for-sale / hosting-default page detector (ADOPT #13, docs/cleanroom/parking_page_detector.md).

    v = incident_verdict(inc, ctx)    # Verdict(cls="parked"|"for_sale"|"hosting_default"|None, score, indicators)

A parking provider serves one template to thousands of unrelated domains, so DOM, title, asset and favicon genes
of a parked capture describe the provider, not the operator. When the incident is parked, `inf_dom` re-tiers its
kit/platform genes to `inf:boilerplate:parked` (0 bits) and emits gene `inf_parked` (label tier, 0 bits, the
satisfied indicators in the evidence note); `inf_har` and `inf_enrich` re-tier the landing host's server/DNS IPs to
`inf:platform:parked_ip`. Operator-tier genes (tracker ids, contacts, registrant-level facts) stay unchanged.

Rules (high precision: a false "parked" removes evidence), any one decides:
1. parking infrastructure (MISP parking IP list on a capture IP, or parking NS from the replay cache whose cached A
   answer intersects the capture IPs) AND fewer than 1500 alphanumeric characters of visible text;
2. a parking / for-sale phrase in the title;
3. two or more distinct phrases AND link text >= 0.5 of all text (sponsored-listing pages are mostly links);
4. the body is a single iframe whose host is a known parking / marketplace provider;
5. a provider script, frame or link (ad feed for parked domains, parking or marketplace host) AND (one phrase or
   thin text).
Hosting-default pages ("Welcome to nginx", Apache test page, registrar "future home") form class `hosting_default`
(phrase in the title, or in thin text). Missing HTML = undecided (None): nothing is downgraded.
Scam landers that are deliberately thin (cloaked white pages) have no phrases and no parking infrastructure, so
rule 1 needs the infrastructure hit. No live DNS: only the replay cache is read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from hydra.contracts import Gene


def _host(url: str) -> str:
    """Lower-case hostname, '' for malformed URLs (urlsplit raises ValueError on e.g. 'http://[bad')"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


THIN_TEXT = 1500  # alphanumeric characters
LINK_RATIO = 0.5
MIN_PHRASES = 2
PARKED_GROUP = "inf:boilerplate:parked"
PARKED_IP_GROUP = "inf:platform:parked_ip"
HOSTING_THIN = 800  # a hosting-default phrase outside the title counts only on a near-empty page
# phrases a sales or scam page can carry in its title too: they count in the body rules, never in the title rule
GENERIC_IN_TITLE = {"make an offer", "zloz oferte", "related links", "sponsored links", "linki sponsorowane"}

# Own phrase lists (folded: lower case, no diacritics). Parking: the domain is parked / shows sponsored results.
PARK_PHRASES = (
    "this domain is parked", "domain is parked", "parked free", "parked domain", "domain parking",
    "related searches", "sponsored listings", "sponsored links", "related links", "domain owner using",
    "ta domena jest zaparkowana", "domena zaparkowana", "domena jest zaparkowana", "parkowanie domen",
    "powiazane wyszukiwania", "linki sponsorowane", "diese domain wurde geparkt", "domain geparkt",
    "gesponserte links", "ce domaine est gare", "liens sponsorises", "dominio aparcado", "enlaces patrocinados",
)
SALE_PHRASES = (
    "domain is for sale", "domain for sale", "may be for sale", "buy this domain", "make an offer",
    "is available for purchase", "inquire about this domain", "get this domain", "purchase this domain",
    "domena na sprzedaz", "domena jest na sprzedaz", "ta domena moze byc na sprzedaz", "kup te domene",
    "kup domene", "zloz oferte", "domena do kupienia", "domain kaufen", "diese domain steht zum verkauf",
    "domain zu verkaufen", "ce domaine est a vendre", "dominio en venta", "este dominio esta a la venta",
)
HOSTING_PHRASES = (
    "welcome to nginx", "apache2 ubuntu default page", "apache2 debian default page", "test page for the apache",
    "it works!", "default web site page", "future home of something quite cool", "web server's default page",
    "this is the default welcome page", "litespeed web server", "default plesk page", "iis windows server",
    "domena zostala poprawnie skonfigurowana", "strona w budowie - serwer",
)
# Hosts / URL fragments of parking networks, domain marketplaces and the parked-domain ad feed (own list, public
# provider names). A match on a script, iframe or link of the page is the "provider marker".
PROVIDER_MARKERS = (
    "sedoparking.com", "sedo.com", "parkingcrew.net", "bodis.com", "above.com", "parklogic.com", "dsparking.com",
    "parkingpage.namecheap.com", "parking-lander", "afternic.com", "dan.com", "hugedomains.com", "undeveloped.com",
    "domainmarket.com", "brandbucket.com", "squadhelp.com", "efty.com", "uniregistry.com", "sav.com/park",
    "domainsponsor", "skenzo.com", "trafficz.com", "smartname.com", "parked.com", "/adsense/domains/caf.js",
    "domainnamesales.com", "buydomains.com", "aftermarket.pl/domena", "parkingpanel",
)
SALE_MARKERS = ("afternic.com", "dan.com", "hugedomains.com", "undeveloped.com", "domainmarket.com",
                "brandbucket.com", "squadhelp.com", "efty.com", "buydomains.com", "domainnamesales.com",
                "aftermarket.pl/domena")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().replace("ł", "l"))
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c)))


def _hits(text: str, phrases: tuple[str, ...]) -> list[str]:
    return [p for p in phrases if p in text]


def _marker(url: str) -> str | None:
    low = url.lower()
    host = _host(low if "//" in low else "//" + low)
    for m in PROVIDER_MARKERS:
        if "/" in m or "." not in m or m.endswith(".js"):
            if m in low:
                return m
        elif host == m or host.endswith("." + m):
            return m
    return None


@dataclass
class Verdict:
    cls: str | None  # parked | for_sale | hosting_default | None (not parked)
    score: float = 0.0
    indicators: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    artifact_id: str | None = None

    @property
    def parked(self) -> bool:
        return self.cls is not None

    def note(self) -> str:
        return f"{self.cls}: rules {','.join(self.rules)}; indicators {', '.join(self.indicators)}; score {self.score}"


def page_verdict(raw: str, landing_host: str | None = None, *, infra: str | None = None,
                 final_host: str | None = None, artifact_id: str | None = None) -> Verdict:
    """Decide one saved page. `infra`: name of the parking-infrastructure hit that is valid for this capture."""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(raw or "", "lxml")
    except Exception:  # noqa: BLE001 — unparsable page: undecided
        return Verdict(None, artifact_id=artifact_id)
    title = _fold(soup.title.get_text(" ", strip=True)) if soup.title else ""
    srcs = [str(t.get("src")) for t in soup.find_all(["script", "iframe", "frame", "img"]) if t.get("src")]
    hrefs = [str(a.get("href")) for a in soup.find_all("a", href=True)]
    iframes = soup.find_all(["iframe", "frame"])
    # read iframe srcs before decompose(): a decomposed ancestor (e.g. <noscript><iframe>) leaves its tags with
    # attrs=None, and .get() on them raised AttributeError (1 in ~400 PhreshPhish pages, TRAIN 2026-11-21)
    iframe_srcs = [str(f.get("src") or "") for f in iframes]
    for t in soup(["script", "style", "noscript", "template"]):
        t.decompose()
    text = _fold(soup.get_text(" ", strip=True))
    alnum = sum(c.isalnum() for c in text)
    link_chars = sum(sum(c.isalnum() for c in a.get_text(" ", strip=True)) for a in soup.find_all("a"))
    ratio = link_chars / alnum if alnum else 0.0
    park, sale, hosting = _hits(text, PARK_PHRASES), _hits(text, SALE_PHRASES), _hits(text, HOSTING_PHRASES)
    title_hit = [p for p in _hits(title, PARK_PHRASES + SALE_PHRASES) if p not in GENERIC_IN_TITLE]
    markers = sorted({m for u in srcs + hrefs if (m := _marker(u))})
    body = soup.body
    visible_children = [c for c in (body.find_all(recursive=False) if body else []) if c.name not in ("br",)]
    single_iframe = bool(iframes) and len(iframes) == 1 and alnum < 200 and len(visible_children) <= 2
    iframe_marker = _marker(iframe_srcs[0]) if single_iframe else None
    thin = alnum < THIN_TEXT
    host = (landing_host or "").lower()
    ind: dict[str, bool] = {
        "parking_infra": bool(infra),
        "thin_text": thin,
        "title_phrase": bool(title_hit),
        f"phrases>={MIN_PHRASES}": len(park) + len(sale) >= MIN_PHRASES,
        f"link_ratio>={LINK_RATIO}": ratio >= LINK_RATIO,
        "single_iframe": single_iframe,
        "provider_marker": bool(markers),
        "host_in_title": bool(host) and host.removeprefix("www.") in title,
        "offsite_final": bool(final_host and host and final_host.lower().removeprefix("www.")
                              != host.removeprefix("www.")),
    }
    rules = []
    if infra and thin:
        rules.append("R1")
    if title_hit:
        rules.append("R2")
    if ind[f"phrases>={MIN_PHRASES}"] and ind[f"link_ratio>={LINK_RATIO}"]:
        rules.append("R3")
    if iframe_marker:
        rules.append("R4")
    if markers and (park or sale or thin):
        rules.append("R5")
    hosting_rule = bool(_hits(title, HOSTING_PHRASES)) or (bool(hosting) and alnum < HOSTING_THIN)
    detail = (
        [f"infra:{infra}"] * bool(infra) + [f"phrase:{p}" for p in (title_hit or (park + sale))[:4]]
        + [f"marker:{m}" for m in markers[:3]] + [f"alnum:{alnum}", f"link_ratio:{ratio:.2f}"]
    )
    score = round(sum(ind.values()) / len(ind), 2)
    if rules:
        cls = "for_sale" if (len(sale) > len(park) or any(m in SALE_MARKERS for m in markers)) else "parked"
        return Verdict(cls, score, [k for k, v in ind.items() if v] + detail, rules, artifact_id)
    if hosting_rule:
        return Verdict("hosting_default", score, [f"hosting:{h}" for h in hosting[:2]] + detail, ["H1"], artifact_id)
    return Verdict(None, score, [k for k, v in ind.items() if v], [], artifact_id)


# ------------------------------------------------------------------------------------------- incident level

_CACHE: dict[tuple, Verdict | None] = {}


def _infra_hit(enr_dir, landing: str, capture_ips: set[str]) -> str | None:
    """Parking IP on a capture IP, or parking NS (replay cache) whose cached A answers meet the capture IPs."""
    from hydra.enrich import warninglists

    for ip in sorted(capture_ips):
        hit = warninglists.classify("dns_ip", ip)
        if hit and hit.effect == "parked_domain":
            return f"{hit.list_name}:{ip}"
    if enr_dir is None or not capture_ips:
        return None
    from hydra.enrich import dns
    from hydra.enrich.client import Enricher
    from hydra.enrich.domains import icann_domain

    enr = Enricher(cache_dir=enr_dir, live=False)  # scoring never goes online
    dom = icann_domain(landing) or landing
    resolved = set(dns.answers(dns.query(enr, landing, "A"))) | set(dns.answers(dns.query(enr, landing, "AAAA")))
    if not resolved & capture_ips:
        return None
    for ns in dns.answers(dns.query(enr, dom, "NS")):
        hit = warninglists.classify("ns_host", ns.lower().rstrip("."))
        if hit and hit.effect == "parked_domain":
            return f"{hit.list_name}:{ns.lower().rstrip('.')}"
    return None


def incident_verdict(inc, ctx=None) -> Verdict | None:
    """Parked verdict of the incident's landing captures; None when there is no HTML (undecided) or not parked.

    The incident is parked when at least one landing page is parked and parked pages are not outnumbered by
    non-parked ones. Memoised per (incident, artifacts) because three extractors ask."""
    from hydra.enrich.domains import is_boilerplate
    from hydra.genome.webparse import bundle

    key = (inc.incident.id, tuple(a.meta.artifact_id for a in inc.artifacts))
    if key in _CACHE:
        return _CACHE[key]
    b = bundle(inc)
    hosts = [_host(u) for u, _, _ in b.landing_urls]
    hosts = [h for h in hosts if h]
    landing = hosts[0] if hosts else None
    final = hosts[-1] if hosts else None
    pages: list[tuple[str, str | None, str | None]] = [(d.raw, d.artifact_id, None) for d in b.htmls if d.raw]
    cap_ips: set[str] = set()
    for h in b.hars:
        if h.local_replay:
            continue
        for body in h.bodies:
            bh = _host(body.url)
            if body.mime == "text/html" and body.data and not is_boilerplate(bh):
                pages.append((body.data.decode("utf-8", "replace"), h.artifact_id, bh))
        if landing and landing in h.hosts:
            cap_ips |= set(h.hosts[landing].ips)
    if not pages:
        _CACHE[key] = None
        return None
    enr_dir = None
    if ctx is not None:
        from hydra.enrich import get_enricher

        try:
            enr_dir = get_enricher(getattr(ctx, "enrich", None), getattr(ctx, "settings", None)).cache_dir
        except Exception:  # noqa: BLE001 — no cache configured: infrastructure rule cannot fire
            enr_dir = None
    infra = _infra_hit(enr_dir, landing, cap_ips) if landing else None
    verdicts = [page_verdict(raw[:2_000_000], ph or landing, infra=infra, final_host=final, artifact_id=aid)
                for raw, aid, ph in pages[:20]]
    parked = [v for v in verdicts if v.parked]
    out = parked[0] if parked and len(parked) >= len(verdicts) - len(parked) else None
    while len(_CACHE) > 256:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = out
    return out


KEEP_KINDS = {"tracker_id", "entity_phone", "entity_email", "entity_iban", "entity_email_domain", "lead_endpoint",
              "capi_token_hash", "operator_id", "tds_value"}


def downgrade_template(genes: list[Gene], v: Verdict) -> int:
    """Kit / platform genes of a parked capture -> `inf:boilerplate:parked` (0 bits). Returns the count."""
    n = 0
    for g in genes:
        parts = (g.dependency_group or "").split(":")
        if g.value is None or g.kind in KEEP_KINDS or len(parts) < 3 or parts[1] not in ("kit", "platform"):
            continue
        g.dependency_group = PARKED_GROUP
        g.quality = min(g.quality, 0.05)
        tag = f"[parked page template: {v.cls}]"
        g.evidence.note = f"{g.evidence.note} {tag}"[:300] if g.evidence.note else tag
        n += 1
    return n


def downgrade_ips(genes: list[Gene], v: Verdict, kinds: tuple[str, ...]) -> int:
    """Operator-tier IP genes of a parked capture belong to the parking provider -> platform tier."""
    n = 0
    for g in genes:
        if g.kind in kinds and g.value and (g.dependency_group or "").startswith("inf:operator:"):
            g.dependency_group = PARKED_IP_GROUP
            g.quality = min(g.quality, 0.3)
            tag = f"[parked page: {v.cls}, provider infrastructure]"
            g.evidence.note = f"{g.evidence.note} {tag}"[:300] if g.evidence.note else tag
            n += 1
    return n
