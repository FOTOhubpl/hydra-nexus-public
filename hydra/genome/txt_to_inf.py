"""OCR/ASR-read URLs and domains as INF landing genes (GAP-AUDIT B7, review M5).

In screenshot mode `pipeline.analyse_input` takes the URL/domain genes read from the image and follows the first
one as a URL input, so the item gets `entry_url` / `landing_host` / `registrable_domain` genes and links by domain.
In file and batch mode (`hydra analyse`, `assign-batch`) that branch never runs, so a screenshot of an ad or a landing
page could not link by its visible domain. Extractor `inf_text_hosts` does the same offline: when the incident has
no landing URL of its own (no HAR entry, saved-from URL, URL file or capture manifest), the best URL read by OCR/ASR
goes through `har.url_genes`, the same function `inf_url` uses for a URL input, and the genes get quality <= 0.8,
provenance INF and an "ocr" note with the OCR box as evidence. Platform, boilerplate and impersonated-brand domains
are skipped; MISP warninglists still apply through `GeneSink`.

Ranking: a URL with a path beats a bare domain (the screenshot branch ranks by quality and may pick the bare
domain), then text outside feed/library-panel zones, then OCR line quality, then reading order. Browser chrome is
kept: the address bar of a landing screenshot is where the domain is. Only the first candidate is used, as in
screenshot mode, so a screenshot gives the same landing genes in both modes.
"""

from __future__ import annotations

from hydra.contracts import Family, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.txt import NOISE_ZONES, _ev
from hydra.genome.txt_entities import find_hosts
from hydra.genome.txt_sources import collect

TEXT_HOST_SOURCES = ("ocr", "asr")
OCR_LANDING_QUALITY = 0.8
VERSION = 4  # 1: every OCR host; 2: first URL via url_genes, no own landing; 3: browser chrome kept; 4: ad ids


def _brand_domain(rd: str) -> bool:
    """The impersonated brand's own domain (onet.pl, tvn24.pl, ...): shown in the creative, not the operator's."""
    from hydra.genome.dom import BRANDS
    from hydra.genome.lexicons import INSTITUTION_BRANDS, MEDIA_BRANDS

    label, _, tld = rd.partition(".")
    names = {k.split(".")[0].replace("_", "") for k in BRANDS}
    names |= {t.id.split("_")[0] for t in (*MEDIA_BRANDS, *INSTITUTION_BRANDS)}  # zus.pl, pzu.pl, gazeta.pl, ...
    names |= {"sport", "wiadomosci", "biznes", "kobieta", "moto", "zdrowie"}  # section domains of news portals
    return label.replace("-", "") in names and tld in ("pl", "com", "eu", "info", "net", "org", "co.uk", "com.pl")


def candidates(inc: ResolvedIncident, ctx: ExtractContext) -> list[tuple[tuple, str, object, object, object]]:
    """[(rank key, url, segment, block, entity)] for hosts read by OCR/ASR, best first."""
    from urllib.parse import urlsplit

    from hydra.enrich.domains import is_boilerplate, registrable_domain, valid_host

    out = []
    order = 0
    for b in collect(inc, ctx).blocks:
        if not b.source.startswith(TEXT_HOST_SOURCES) or b.channel not in ("ad", "page", "text"):
            continue
        for s in b.segments:
            if not s.text:
                continue  # browser chrome stays in: the address bar of a landing screenshot is the best evidence
            noise = 1 if s.zone in NOISE_ZONES else 0
            for e in find_hosts(s.text):
                if e.kind == "url_in_text":
                    host = (urlsplit(e.value).hostname or "").lower().removeprefix("www.")
                    url, has_path = e.value, True
                elif e.kind == "domain_in_text":
                    host, url, has_path = e.value, f"https://{e.value}/", False
                else:
                    continue
                rd = registrable_domain(host)
                if not valid_host(host) or is_boilerplate(host) or not rd or _brand_domain(rd):
                    continue
                q = (s.quality or 1.0) * e.quality
                out.append(((0 if has_path else 1, noise, -q, order), url, s, b, e))
                order += 1
    out.sort(key=lambda x: x[0])
    return out


@register("inf_text_hosts", family=Family.INF, version=VERSION)
def inf_text_hosts(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    from hydra.genome.har import url_genes
    from hydra.genome.webparse import GeneSink, bundle

    sink = GeneSink(inc.incident.id, f"inf_text_hosts@{VERSION}")
    if bundle(inc).landing_urls:
        sink.missing("landing_host", GeneStatus.NOT_APPLICABLE,
                     "incident has its own landing URL; OCR hosts stay TXT evidence", "url")
        return sink.result()
    cands = candidates(inc, ctx)
    if not cands:
        sink.missing("landing_host", GeneStatus.NOT_APPLICABLE, "no host read by OCR/ASR", "url")
        return sink.result()
    _, url, s, b, e = cands[0]
    url_genes(sink, url, f"ocr:{b.source}", b.artifact_id)
    shown = s.text[e.start:e.end]
    for g in sink.genes:
        g.quality = round(min(g.quality, OCR_LANDING_QUALITY * (s.quality or 1.0)), 3)
        g.provenance = Provenance.INF
        note = f"{g.evidence.note or ''} [ocr {b.channel}: {shown}]".strip()[:300]
        g.evidence = _ev(s, e.start, e.end, note)
    return sink.result()
