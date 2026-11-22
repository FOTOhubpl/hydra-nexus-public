"""Public phishing feeds -> HYDRA incidents (metadata only; the scam pages are never fetched).

NASK task §8 allows extra public data if its origin is stated. This adapter turns rows of three public feeds into
incidents for a separate PUBLIC demo corpus (own state dir, never mixed with organiser data):

| source key | feed | licence / terms (read 2026-11-21) |
|---|---|---|
| `cert_pl` | CERT Polska warning list, hole.cert.pl/domains/v2/domains.json | free use, no endorsement |
| `phishtank` | PhishTank online-valid.csv, data.phishtank.com/data/online-valid.csv | free, attribution |
| `phiusiil` | PhiUSIIL Phishing URL Dataset, UCI ML Repository id 967 (Prasad & Chandra 2024) | CC BY 4.0 |

OpenPhish is excluded (its terms forbid showing derived results); Smishing IMC25 has no URLs per row we may show.

One incident per landing host (rows with the same host are merged; every source and first-seen date is kept in
`Incident.meta`). Each incident has one `url` role artifact (`url.txt`: up to 5 URLs of the host; a CERT Polska
entry is a bare domain, so its URL is synthesised as `http://<domain>/` and flagged `url_synthesised`). The genes
come from the normal extractors (`inf_url`, `inf_enrich`) plus `inf_feed` (below): no new linking rule, only two
URL-derived genes that the shape-only `url_label_skeleton` drops (brand word, digit-masked label skeleton).

`inf_feed` registers itself when this module is imported (only `scripts/build_public_corpus.py` does), so runs on
organiser data are unchanged.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hydra import events
from hydra.contracts import ArtifactRole, Family, GeneStatus, Incident, IncidentArtifact
from hydra.db import Writer
from hydra.enrich.domains import is_boilerplate, split_host, valid_host
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.jobs import new_job_id
from hydra.redact import redact_url
from hydra.storage import ContentStore

SOURCES: dict[str, dict[str, str]] = {
    "cert_pl": {
        "name": "CERT Polska - Lista ostrzeżeń (domeny)",
        "url": "https://hole.cert.pl/domains/v2/domains.json",
        "licence": "public list, free use; must not imply endorsement by CERT Polska",
    },
    "phishtank": {
        "name": "PhishTank online-valid (Cisco Talos)",
        "url": "http://data.phishtank.com/data/online-valid.csv",
        "licence": "free download and use, attribution to PhishTank required",
    },
    "phiusiil": {
        "name": "PhiUSIIL Phishing URL Dataset (Prasad & Chandra 2024, UCI id 967)",
        "url": "https://archive.ics.uci.edu/dataset/967/phiusiil+phishing+url+dataset",
        "licence": "CC BY 4.0",
    },
}

# Polish brands impersonated in phishing (guess from the host name only). Long names match as substrings,
# short ones (<= 4 chars, collide with English words) only as a whole host token or a token prefix listed here.
PL_BRANDS: list[tuple[str, str]] = [
    ("allegro", r"allegro"), ("inpost", r"inpost|in-post|paczkomat"), ("olx", r"olx"),
    ("vinted", r"vinted"), ("otomoto", r"otomoto"), ("pko", r"pko|ipko|pkobp"), ("mbank", r"mbank"),
    ("ing", r"ing|ingbank|ing-bank|ingbs"), ("santander", r"santander"), ("pekao", r"pekao|peopay"),
    ("millennium", r"millennium|millenium"), ("alior", r"alior"), ("bnp", r"bnp|bnpparibas"),
    ("velobank", r"velobank|velo-bank"), ("credit-agricole", r"credit-?agricole"), ("blik", r"blik"),
    ("poczta-polska", r"poczta|pocztex|poczta-polska|pocztapolska|envelo"), ("dhl", r"dhl"), ("dpd", r"dpd"),
    ("gls", r"gls"), ("orlen", r"orlen"), ("pge", r"pge"), ("tauron", r"tauron"), ("energa", r"energa"),
    ("mobywatel", r"mobywatel|m-obywatel"), ("epuap", r"epuap"), ("gov.pl", r"gov-pl|govpl|gov\.pl"),
    ("zus", r"zus"), ("podatki", r"podatki|e-urzad|eurzad"), ("biedronka", r"biedronka"), ("lidl", r"lidl"),
    ("orange", r"orange"), ("play", r"play"), ("plus", r"plus"), ("t-mobile", r"t-?mobile"),
]
SHORT_TOKEN_BRANDS = {"olx", "pko", "ing", "bnp", "blik", "dhl", "dpd", "gls", "pge", "zus", "play", "plus", "lidl"}
# Generic Polish lure words (delivery, payment, fine) in the host: keywords, not brands.
PL_LURE_WORDS = re.compile(
    r"paczk|przesylk|przesyłk|dostaw|platnos|płatnoś|oplat|opłat|doplat|dopłat|zamowien|zamówien|odbior|"
    r"mandat|zwrot|faktur|kurier|weryfikac|logowan|konto-|bezpiecz|polska|-pl-|-pl\.|\.pl-", re.I
)
PL_PHISHTANK_TARGETS = re.compile(
    r"allegro|inpost|olx|pko|mbank|santander|pekao|poczta|vinted|orlen|millennium|alior|bnp|ing (direct|bank)",
    re.I,
)
MAX_URLS_PER_HOST = 5
# Incident.source tag per feed (corpus flag agreed with the architect, 2026-11-21): the primary source of a merged
# host is the first in this order; every source stays in `meta.feed_sources`.
SOURCE_TAG = {"cert_pl": "public:certpl", "phishtank": "public:phishtank", "phiusiil": "public:phiusiil"}
CORPUS_NAME = "pl-feeds"


def _tokens(host: str) -> list[str]:
    return [t for t in re.split(r"[.\-_0-9]+", host.lower()) if t]


def brand_guess(host: str) -> str | None:
    """First impersonated Polish brand recognised in the host name (a guess for the analyst, not a fact)."""
    h = host.lower()
    toks = _tokens(h)
    for brand, pat in PL_BRANDS:
        if brand in SHORT_TOKEN_BRANDS:
            alts = pat.split("|")
            if any(t == a or (len(a) > 4 and t.startswith(a)) for t in toks for a in alts) or any(
                t.startswith(brand) and len(t) <= len(brand) + 2 and t[len(brand):] in ("", "pl", "bp")
                for t in toks
            ):
                return brand
        elif re.search(pat, h):
            return brand
    return None


def is_pl_targeted(host: str, target: str | None = None) -> bool:
    return (
        host.endswith(".pl")
        or brand_guess(host) is not None
        or bool(PL_LURE_WORDS.search(host))
        or bool(target and PL_PHISHTANK_TARGETS.search(target))
    )


def host_of(url: str) -> str:
    u = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "http://" + u
    try:
        return (urlsplit(u).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def digit_skeleton(host: str) -> str | None:
    """Host labels with digit runs masked: 'allegrolokalnie.pe-7492104.sbs' -> 'allegrolokalnie.pe-#.sbs'.

    The rotated-domain pattern of one kit (scale lab C: pair precision 0.95 on real phishing, against 0.54 for
    the registrable domain). Kept apart from `url_label_skeleton`, which replaces letters by shapes as well."""
    e = split_host(host)
    if not e.suffix or not e.domain:
        return None
    labels = [x for x in e.subdomain.split(".") if x and x != "www"] + [e.domain]
    return ".".join(re.sub(r"\d+", "#", x) for x in labels) + "." + e.suffix


@dataclass
class FeedRow:
    url: str
    host: str
    source: str
    source_id: str
    first_seen: str | None = None
    target: str | None = None  # feed's own target label (PhishTank), weak
    url_synthesised: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------- loaders


def load_cert_pl(path: Path, *, since: str | None = None, active_only: bool = False) -> list[FeedRow]:
    """CERT Polska warning list JSON (list of {RegisterPositionId, DomainAddress, InsertDate, DeleteDate})."""
    rows = []
    for e in json.loads(Path(path).read_text(encoding="utf-8")):
        d = str(e.get("DomainAddress") or "").strip().lower().rstrip(".")
        ins = e.get("InsertDate")
        if not d or (since and (not ins or ins < since)) or (active_only and e.get("DeleteDate")):
            continue
        rows.append(FeedRow(url=f"http://{d}/", host=d, source="cert_pl", source_id=str(e.get("RegisterPositionId")),
                            first_seen=ins, url_synthesised=True,
                            extra={"deleted": e.get("DeleteDate")} if e.get("DeleteDate") else {}))
    return rows


def load_phishtank(path: Path, *, pl_only: bool = True) -> list[FeedRow]:
    rows = []
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for r in csv.DictReader(f):
            url, host = r.get("url") or "", host_of(r.get("url") or "")
            target = (r.get("target") or "").strip() or None
            if not host or (pl_only and not is_pl_targeted(host, target)):
                continue
            rows.append(FeedRow(url=url, host=host, source="phishtank", source_id=str(r.get("phish_id")),
                                first_seen=r.get("submission_time") or None,
                                target=None if target == "Other" else target))
    return rows


def load_phiusiil(path: Path, *, pl_only: bool = True) -> list[FeedRow]:
    """PhiUSIIL CSV; label 0 = phishing (dataset card). No per-row dates."""
    csv.field_size_limit(10**8)
    rows = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        for i, r in enumerate(csv.DictReader(f)):
            if str(r.get("label")) != "0":
                continue
            url, host = r.get("URL") or "", host_of(r.get("URL") or "")
            if not host or (pl_only and not is_pl_targeted(host)):
                continue
            rows.append(FeedRow(url=url, host=host, source="phiusiil", source_id=str(r.get("FILENAME") or i)))
    return rows


# --------------------------------------------------------------------------------------- incidents


def incident_id(host: str) -> str:
    return "pub-" + hashlib.sha256(host.encode()).hexdigest()[:10]


@dataclass
class HostGroup:
    host: str
    rows: list[FeedRow]

    @property
    def first_seen(self) -> str | None:
        ds = sorted(r.first_seen for r in self.rows if r.first_seen)
        return ds[0] if ds else None

    @property
    def sources(self) -> list[str]:
        return sorted({r.source for r in self.rows})


def group_by_host(rows: Iterable[FeedRow]) -> list[HostGroup]:
    """Merge rows per landing host; drop invalid and boilerplate hosts (a shared platform host is not one case)."""
    by: dict[str, list[FeedRow]] = {}
    for r in rows:
        if valid_host(r.host) and not is_boilerplate(r.host):
            by.setdefault(r.host, []).append(r)
    return [HostGroup(h, rs) for h, rs in sorted(by.items())]


def to_incident(g: HostGroup, store: ContentStore) -> Incident:
    """One incident per host: a `url` artifact (stored in the CAS) + feed metadata. Never fetches anything."""
    real = [r.url for r in g.rows if not r.url_synthesised]
    urls = list(dict.fromkeys(redact_url(u.strip()) for u in (real or [g.rows[0].url])))[:MAX_URLS_PER_HOST]
    urls = [u if re.match(r"^https?://", u, re.I) else "http://" + u for u in urls]
    data = ("\n".join(urls) + "\n").encode()
    digest = store.put_bytes(data)
    targets = sorted({r.target for r in g.rows if r.target})
    brand = brand_guess(g.host)
    art = IncidentArtifact(artifact_id=digest, role=ArtifactRole.URL, path_in_incident="url.txt",
                           mime="text/plain", size=len(data), meta={"url": urls[0]})
    meta = {
        "url": urls[0],
        "public_feed": True,
        "feed_sources": g.sources,
        "feed_refs": [{"source": r.source, "id": r.source_id, "first_seen": r.first_seen} for r in g.rows[:20]],
        "feed_rows": len(g.rows),
        "first_seen": g.first_seen,
        "brand_guess": brand,
        "feed_targets": targets,
        "url_synthesised": not real,
        "licences": {s: SOURCES[s]["licence"] for s in g.sources},
    }
    if any(r.extra.get("deleted") for r in g.rows):
        meta["cert_pl_deleted"] = True
    title = f"{g.host}" + (f" ({brand})" if brand else "")
    primary = next(k for k in SOURCE_TAG if k in g.sources)
    meta["corpus"] = CORPUS_NAME
    return Incident(id=incident_id(g.host), source=SOURCE_TAG[primary],
                    source_ref=SOURCES[primary]["url"] + f"#{next(r.source_id for r in g.rows if r.source == primary)}",
                    title=title,
                    observed_at=g.first_seen, artifacts=[art], meta=meta)


class _HashOnlyStore:
    """Stand-in for ContentStore when the caller only wants Incident objects (ids are still the sha256)."""

    @staticmethod
    def put_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


def load_groups_jsonl(path: Path) -> list[HostGroup]:
    out = []
    for line in Path(path).read_text().splitlines():
        j = json.loads(line)
        out.append(HostGroup(j["host"], [FeedRow(**r) for r in j["rows"]]))
    return out


def iter_incidents(root: Path, limit: int | None = None, *, store: ContentStore | None = None,
                   cert_since: str | None = "2026-11-18") -> Iterator[Incident]:
    """Adapter entry point (same style as `hydra.adapters.datasets`): incidents of the public feed corpus.

    `root` is either a built corpus dir (`<data>/public-corpus/<name>/` holding `groups.jsonl`, written by
    `scripts/build_public_corpus.py select`) or a data dir with the raw feeds (`external/cert/domains.json`,
    `external-lab/phishtank/online-valid.csv`, `external-lab/phiusiil/PhiUSIIL_Phishing_URL_Dataset.csv`).
    With `store` the url.txt bytes go to the CAS (needed before extraction); without it only ids are computed."""
    root = Path(root).expanduser()
    if (root / "groups.jsonl").is_file():
        groups = load_groups_jsonl(root / "groups.jsonl")
    else:
        rows: list[FeedRow] = []
        cert = root / "external" / "cert" / "domains.json"
        pt = root / "external-lab" / "phishtank" / "online-valid.csv"
        ph = root / "external-lab" / "phiusiil" / "PhiUSIIL_Phishing_URL_Dataset.csv"
        if cert.is_file():
            rows += load_cert_pl(cert, since=cert_since)
        if pt.is_file():
            rows += load_phishtank(pt)
        if ph.is_file():
            rows += load_phiusiil(ph)
        groups = group_by_host(rows)
    st = store or _HashOnlyStore()
    for g in groups[:limit] if limit else groups:
        yield to_incident(g, st)  # type: ignore[arg-type]


def import_groups(groups: list[HostGroup], writer: Writer, store: ContentStore, batch: int = 500) -> list[Incident]:
    """Write incidents through the single Writer, in batches (one transaction per batch)."""
    from hydra.adapters.nask_folder import _write_incident

    out: list[Incident] = []
    for s in range(0, len(groups), batch):
        incs = [to_incident(g, store) for g in groups[s : s + batch]]

        def _w(conn, incs=incs) -> None:
            for inc in incs:
                _write_incident(conn, inc, new_job_id())

        writer.run(_w, timeout=600)
        out.extend(incs)
    return out


# --------------------------------------------------------------------------------------- inf_feed genes


@register("inf_feed", family=Family.INF, version=1)
def inf_feed(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """URL-derived genes for feed incidents: impersonated brand word (kit tier, never a campaign link alone) and
    the digit-masked label skeleton (operator tier, same bit budget as `url_label_skeleton`)."""
    from hydra.genome.webparse import GeneSink

    sink = GeneSink(inc.incident.id, "inf_feed@1")
    if not inc.incident.meta.get("public_feed"):
        return sink.result()
    hosts: list[tuple[str, str | None]] = []
    for a in inc.artifacts:
        if a.meta.role != "url":
            continue
        try:
            for line in a.path.read_text(errors="replace").splitlines()[:MAX_URLS_PER_HOST]:
                if (h := host_of(line)) and valid_host(h):
                    hosts.append((h, a.meta.artifact_id))
        except OSError:
            continue
    for h, aid in dict.fromkeys(hosts):
        b = brand_guess(h)
        if b:
            sink.add("url_brand", b, "inf:kit:brand", artifact_id=aid, note=h, quality=0.3)
        sk = digit_skeleton(h)
        if sk and "#" in sk:  # without digits it equals the host itself (already a landing gene)
            sink.add("url_digit_skeleton", sk, "inf:operator:url_skeleton", artifact_id=aid, note=h, quality=0.8)
    if not sink.genes:
        sink.missing("url_brand", GeneStatus.NOT_APPLICABLE, "no brand word or digit pattern in the host",
                     "inf:kit:brand")
    return sink.result()


def describe_sources() -> str:
    return events.canonical_json(SOURCES)
