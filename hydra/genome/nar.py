"""NAR family: scheme type, motifs, persona, promise, CTA type, urgency, impersonated brands, decoy copy.

Lexicon rules (hydra/genome/lexicons) on folded text, with evidence spans (artifact + bbox/dom_path +
char range). Scheme scores are rule sums, not probabilities; `quality` = min(1, score / 2*threshold).
Narrative genes are typical of a scheme family, so they are "similar campaign" evidence and get low
weight for "same campaign" in the scorer (CAMPAIGNS.md: scheme + brand + persons are weak alone).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.lexicons import (
    CTAS,
    FUNNEL_LURE_MOTIFS,
    FUNNEL_STAGES,
    FUNNEL_TRUST_MOTIFS,
    FUNNEL_TRUST_PERSONAS,
    HANDOFF_TERMS,
    INSTITUTION_BRANDS,
    MEDIA_BRANDS,
    MOTIFS,
    PAYMENT_TERMS,
    PERSONAS,
    PROMISES,
    SCHEME_MIN_SCORE,
    SCHEMES,
    SE_LABEL_PL,
    SE_TECHNIQUES,
    SE_TERMS,
    UPPER_ONLY_BRANDS,
    URGENCY,
    Term,
    compile_terms,
)
from hydra.genome.txt import NOISE_ZONES, _ev, _gene, dedupe
from hydra.genome.txt_norm import fold, guess_language, tokens
from hydra.genome.txt_sources import IncidentTexts, Segment, TextBlock, collect

# 2: PL-SCAM public lexicons (schemes, motifs, brands) on integration, and separately se_technique genes and the
# funds_recovery scheme (G12) on feat/variants-pipe; 3: both merged (feat/merge-batch3), so neither nar@2 cache
# is reused for the combined extractor.
VERSION = 3


def _segments(texts: IncidentTexts, include_chrome: bool = False) -> list[tuple[TextBlock, Segment]]:
    out = []
    for b in texts.blocks:
        if b.source in ("html_commenter", "html_meta", "html_nav"):
            continue
        for s in b.segments:
            if s.zone in NOISE_ZONES or (s.chrome and not include_chrome) or not s.text:
                continue
            out.append((b, s))
    return out


def match_terms(terms: tuple[Term, ...], segs: list[tuple[TextBlock, Segment]]) -> dict[str, tuple[Term, Segment,
                                                                                                    int, int, int]]:
    """term id -> (term, first segment, start, end, hit count)."""
    hits: dict[str, tuple[Term, Segment, int, int, int]] = {}
    compiled = compile_terms(terms)
    for _, s in segs:
        f = fold(s.text)
        for t, rx in compiled:
            for m in rx.finditer(f):
                if t.id in hits:
                    h = hits[t.id]
                    hits[t.id] = (h[0], h[1], h[2], h[3], h[4] + 1)
                else:
                    hits[t.id] = (t, s, m.start(), m.end(), 1)
    return hits


def scheme_scores(segs: list[tuple[TextBlock, Segment]]) -> dict[str, tuple[float, dict]]:
    out = {}
    for scheme, terms in SCHEMES.items():
        hits = match_terms(terms, segs)
        score = sum(h[0].weight * (1.0 if h[4] == 1 else 1.25) for h in hits.values())
        out[scheme] = (round(score, 2), hits)
    return out


def _hit_gene(inc, ex, kind, value, hit, q=0.8, group=None) -> Gene:
    t, s, st, en, n = hit
    origin = f"{t.origin}:{t.source}" if t.source else t.origin
    return _gene(inc, ex, kind, value, _ev(s, st, en, f"{s.text[st:en]!r} (x{n}, {origin})"), q,
                 group or f"nar_{kind}", Provenance.INF, family=Family.NAR)


@register("nar", family=Family.NAR, version=VERSION)
def nar(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    ex = "nar"
    texts = collect(inc, ctx)
    segs = _segments(texts)
    if not segs:
        return ExtractResult()
    genes: list[Gene] = []
    # ---- scheme type
    scores = scheme_scores(segs)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1][0])
    best_score = ranked[0][1][0]
    emitted = False
    for scheme, (score, hits) in ranked:
        if score < SCHEME_MIN_SCORE or score < 0.5 * best_score:
            continue
        top = max(hits.values(), key=lambda h: h[0].weight)
        t, s, st, en, _ = top
        terms = ", ".join(sorted(hits))
        genes.append(_gene(inc, ex, "scheme", scheme, _ev(s, st, en, f"score {score}: {terms}"),
                           min(1.0, score / (2 * SCHEME_MIN_SCORE)), "nar_scheme", Provenance.INF, family=Family.NAR))
        emitted = True
    if not emitted:
        genes.append(_gene(inc, ex, "scheme", "other", EvidencePointer(note=f"best {ranked[0][0]} {best_score}"),
                           0.3, "nar_scheme", Provenance.INF, family=Family.NAR))
    # ---- motifs, persona, promise, urgency
    by_kind: dict[str, dict] = {}
    for kind, terms in (("motif", MOTIFS), ("persona", PERSONAS), ("promise", PROMISES), ("urgency", URGENCY)):
        by_kind[kind] = match_terms(terms, segs)
        for tid, hit in by_kind[kind].items():
            genes.append(_hit_gene(inc, ex, kind, tid, hit, 0.6 + 0.1 * min(hit[4], 3)))
    # ---- CTA (buttons are platform chrome, so include chrome lines)
    for tid, hit in match_terms(CTAS, _segments(texts, include_chrome=True)).items():
        genes.append(_hit_gene(inc, ex, "cta", tid, hit, 0.8))
    # ---- impersonated media / institutions (incident-level; TXT has per-mention brand_mention genes)
    for kind, terms in (("media_brand", MEDIA_BRANDS), ("institution_brand", INSTITUTION_BRANDS)):
        by_kind[kind] = {}
        for tid, hit in match_terms(terms, segs).items():
            raw = hit[1].text[hit[2]:hit[3]]
            if tid in UPPER_ONLY_BRANDS and not raw.isupper() and len(raw) <= 4:
                continue
            by_kind[kind][tid] = hit
            genes.append(_hit_gene(inc, ex, kind, tid, hit, 0.8))
    # ---- named social-engineering techniques (G12): explanation facts with char-range evidence
    genes.extend(se_technique_genes(inc, ex, segs, by_kind))
    # ---- channel mismatch: ad creative story vs landing story, decoy library copy
    genes.extend(_mismatch_genes(inc, ex, texts))
    return ExtractResult(genes=dedupe(genes))


def se_technique_genes(inc, ex: str, segs, by_kind: dict[str, dict]) -> list[Gene]:
    """One `se_technique` gene per named technique (urgency, scarcity, authority, social_proof, reciprocity,
    liking, fear). Sources: NAR term hits mapped by `SE_TECHNIQUES` and the extra `SE_TERMS`; evidence = the first
    source hit (segment pointer + char_range), the note lists every source. Group `NAR:kit:se_technique`: NAR has
    campaign cap 0, so these never support a campaign link."""
    out = []
    for tech, sources in SE_TECHNIQUES.items():
        found: list[tuple[str, tuple]] = []
        for kind, tid in sources:
            hits = by_kind.get(kind, {})
            for hid, hit in sorted(hits.items()):
                if tid in ("*", hid):
                    found.append((f"{kind}:{hid}", hit))
        for tid, hit in sorted(match_terms(SE_TERMS.get(tech, ()), segs).items()):
            found.append((f"se:{tid}", hit))
        if not found:
            continue
        found.sort(key=lambda x: (-x[1][4], x[0]))
        src, (t, s, st, en, n) = found[0]
        srcs = ", ".join(x[0] for x in found)
        note = f"{SE_LABEL_PL[tech]}: {s.text[st:en]!r} ({src}, x{n}); sources: {srcs}"
        out.append(_gene(inc, ex, "se_technique", tech, _ev(s, st, en, note), min(1.0, 0.5 + 0.1 * len(found)),
                         "nar_se_technique", Provenance.INF, family=Family.NAR))
    return out


def _channel_text(texts: IncidentTexts, pred) -> tuple[str, Segment | None]:
    segs = [s for b in texts.blocks for s in b.segments if pred(b, s) and s.text and not s.chrome]
    return " ".join(s.text for s in segs), (segs[0] if segs else None)


def _mismatch_genes(inc, ex, texts: IncidentTexts) -> list[Gene]:
    """Decoy copy: the Ad Library ad text shares no vocabulary / scheme / language with the creative
    (sample: a gambling creative whose library copy describes bath rugs). Ad vs landing scheme mismatch:
    the ad story (political rage-bait) and the page (investment conference) disagree: a cloaking hint."""
    out = []
    lib, lib_seg = _channel_text(texts, lambda b, s: s.zone == "library_ad")
    creative, cr_seg = _channel_text(texts, lambda b, s: b.channel == "ad" and s.zone == "ad")
    if lib_seg and cr_seg and len(tokens(lib)) >= 6 and len(tokens(creative)) >= 4:
        a = {t.text for t in tokens(lib) if len(t.text) > 3}
        c = {t.text for t in tokens(creative) if len(t.text) > 3}
        jac = len(a & c) / max(1, len(a | c))
        la, _ = guess_language(lib)
        lc, _ = guess_language(creative)
        sa, sc = _top_scheme(lib), _top_scheme(creative)
        if jac < 0.05 and (la != lc or sa != sc):
            out.append(_gene(inc, ex, "decoy_copy", "library_text_mismatch",
                             _ev(lib_seg, note=f"library copy lang={la} scheme={sa} vs creative lang={lc} "
                                               f"scheme={sc}; word Jaccard {jac:.2f}: {lib[:120]!r}"),
                             0.8, "nar_mismatch", Provenance.INF, family=Family.NAR))
    page, pg_seg = _channel_text(texts, lambda b, s: b.channel == "page" and b.source in ("html_body", "html_headline"))
    if cr_seg and pg_seg and len(tokens(creative)) >= 6:
        sc, sp = _top_scheme(creative), _top_scheme(page)
        if sc and sp and sc != sp and "fake_news_bait" not in (sc, sp):
            out.append(_gene(inc, ex, "ad_page_mismatch", f"{sc}->{sp}",
                             _ev(cr_seg, note=f"ad reads as {sc}, landing reads as {sp}"), 0.6, "nar_mismatch",
                             Provenance.INF, family=Family.NAR))
    return out


def _top_scheme(text: str) -> str | None:
    seg = Segment(text, text, EvidencePointer())
    scores = scheme_scores([(None, seg)])  # type: ignore[list-item]
    best = max(scores.items(), key=lambda kv: kv[1][0])
    return best[0] if best[1][0] >= 2.0 else None





# =============================================================================== funnel stages

_FIELD_RX = {
    "phone": re.compile(r"tel|phone|telefon|numer|mobile|msisdn", re.I),
    "email": re.compile(r"e-?mail|mail", re.I),
    "name": re.compile(r"name|imie|imię|nazwisk|fio|first|last|surname", re.I),
    "address": re.compile(r"adres|address|street|ulica|city|miasto|kod_pocztowy|zip", re.I),
}
_CPA_FIELDS = re.compile(r"^(offer|offer_uuid|network|api_key|click_?id|clickid|sub\d|sub_id|subid|sid|x_id|"
                         r"landing_id|tracker|fb_pixel|pixel|stream|flow|campaign_id|aff\w*)$", re.I)
_LEAD_PATH = re.compile(r"order|lead|submit|register|signup|trigger|form|send|callback|api/v\d", re.I)
_SDK_FORM = re.compile(r"\baio-(form|sdk)|aio__country|lead-?form|order_form|orderform", re.I)


def classify_fields(names) -> set[str]:
    out = set()
    for n in names:
        for k, rx in _FIELD_RX.items():
            if n and rx.search(str(n)):
                out.add(k)
    return out


@dataclass
class StageHit:
    stage: str
    signal: str
    evidence: EvidencePointer
    quality: float = 0.7


def _first_hit_ev(hit) -> EvidencePointer:
    t, s, st, en, n = hit
    return _ev(s, st, en, f"{s.text[st:en]!r} (x{n})")


def funnel_hits(inc: ResolvedIncident, ctx: ExtractContext, texts: IncidentTexts,
                nar_genes: list[Gene]) -> list[StageHit]:
    hits: list[StageHit] = []
    by_kind: dict[str, list[Gene]] = {}
    for g in nar_genes:
        by_kind.setdefault(g.kind, []).append(g)

    def from_genes(kind: str, values, stage: str, q: float = 0.7) -> None:
        for g in by_kind.get(kind, []):
            if values is None or g.value in values:
                hits.append(StageHit(stage, f"{kind}:{g.value}", g.evidence, q))

    # lure: the hook (fake news, celebrity, outrage, miracle)
    from_genes("scheme", {"fake_news_bait"}, "lure", 0.8)
    from_genes("motif", FUNNEL_LURE_MOTIFS, "lure")
    from_genes("decoy_copy", None, "lure", 0.5)
    # trust transfer: borrowed media / institution credibility, fake social proof, authority personas
    from_genes("media_brand", None, "trust")
    from_genes("institution_brand", None, "trust", 0.6)
    from_genes("motif", FUNNEL_TRUST_MOTIFS, "trust")
    from_genes("persona", FUNNEL_TRUST_PERSONAS, "trust", 0.6)
    for b in texts.by_source("html_comment"):
        if len(b.segments) >= 2:
            hits.append(StageHit("trust", f"fake_comments:{len(b.segments)}", b.segments[0].evidence, 0.8))
            break
    # offer: product / platform, promise, price
    from_genes("promise", None, "offer")
    from_genes("scheme", {"investment", "health_supplement", "gambling", "giveaway"}, "offer", 0.6)
    from hydra.genome.txt_entities import find_amounts, find_brands

    got_product = got_price = False
    for _b, s in _segments(texts):
        if not got_product:
            for e in find_brands(s.text):
                if e.kind == "product_name":
                    hits.append(StageHit("offer", f"product:{e.value}", _ev(s, e.start, e.end, e.value), 0.9))
                    got_product = True
                    break
        if not got_price:
            for e in find_amounts(s.text):
                if e.kind == "entity_price":
                    hits.append(StageHit("offer", f"price:{e.value}", _ev(s, e.start, e.end, e.value), 0.6))
                    got_price = True
                    break
        if got_product and got_price:
            break
    # capture: lead form fields (static forms, SDK-injected forms, HAR POST bodies)
    _capture_and_handoff(inc, texts, by_kind, hits, ctx)
    # payment / withdrawal pressure
    segs = _segments(texts)
    for tid, hit in match_terms(PAYMENT_TERMS, segs).items():
        q = {"deposit": 0.8, "second_deposit": 0.8, "cash_on_delivery": 0.8, "withdrawal": 0.7}.get(tid, 0.5)
        hits.append(StageHit("payment", f"payment:{tid}", _first_hit_ev(hit), q))
    return hits


def _capture_and_handoff(inc, texts, by_kind, hits: list[StageHit], ctx) -> None:
    from hydra.genome import webparse

    fields: set[str] = set()
    cpa: set[str] = set()
    for a in inc.artifacts:
        role = a.meta.role
        if role == ArtifactRole.LANDING_HTML:
            try:
                d = webparse.parse_html(a)
            except Exception:  # noqa: BLE001 — a broken page is just one less signal
                continue
            for f in d.forms:
                names = list(f.get("fields") or [])
                kinds = classify_fields(names)
                if kinds & {"phone", "email"}:
                    fields |= kinds
                    hits.append(StageHit("capture", "form:" + ",".join(sorted(kinds)),
                                         EvidencePointer(artifact_id=a.meta.artifact_id,
                                                         note=f"form fields {sorted(names)[:12]}"), 0.9))
                    action = str(f.get("action") or "")
                    hidden = [h for h in (f.get("hidden") or []) if _CPA_FIELDS.match(h)]
                    if hidden:
                        cpa |= set(hidden)
                        hits.append(StageHit("handoff", "cpa_hidden_fields", EvidencePointer(
                            artifact_id=a.meta.artifact_id, note=f"hidden {sorted(hidden)[:10]} action={action[:80]}"),
                            0.8))
                    elif _LEAD_PATH.search(action):
                        hits.append(StageHit("handoff", "form_post", EvidencePointer(
                            artifact_id=a.meta.artifact_id, note=f"action={action[:120]}"), 0.6))
            cls = " ".join(d.classes[:4000]) + " " + " ".join(d.ids[:2000])
            m = _SDK_FORM.search(cls)
            if m:
                hits.append(StageHit("capture", f"sdk_form:{m.group(0).lower()}", EvidencePointer(
                    artifact_id=a.meta.artifact_id, note=f"lead-form SDK marker {m.group(0)!r}"), 0.7))
        elif role == ArtifactRole.HAR or a.meta.path_in_incident.lower().endswith(".har"):
            try:
                h = webparse.parse_har(a)
            except Exception:  # noqa: BLE001
                continue
            for p in h.posts:
                kinds = classify_fields(p.get("fields") or [])
                path = str(p.get("path_skel") or "")
                if kinds & {"phone", "email", "name"} or _LEAD_PATH.search(path):
                    ev = EvidencePointer(artifact_id=a.meta.artifact_id, har_entry=p.get("entry"),
                                         note=f"POST {p.get('host')}{path} fields={sorted(p.get('fields') or [])[:10]}")
                    if kinds & {"phone", "email"}:
                        fields |= kinds
                        hits.append(StageHit("capture", "har_post:" + ",".join(sorted(kinds)), ev, 0.9))
                    hits.append(StageHit("handoff", "lead_post", ev, 0.8))
    # INF genes, when the pipeline passes the incident's genes
    for g in ctx.options.get("incident_genes") or []:
        if getattr(g, "kind", None) == "lead_endpoint":
            hits.append(StageHit("handoff", "lead_endpoint", g.evidence, 0.8))
            break
    # text-level signals
    for g in by_kind.get("cta", []):
        if g.value in ("leave_phone", "register", "order"):
            hits.append(StageHit("capture", f"cta:{g.value}", g.evidence, 0.5))
    if texts.by_source("html_form"):
        b = texts.by_source("html_form")[0]
        f = " ".join(s.text for s in b.segments)
        kinds = classify_fields(f.split())
        if kinds & {"phone", "email"} or re.search(r"\d{3}[ -]\d{3}[ -]\d{3}", f):
            hits.append(StageHit("capture", "form_text", b.segments[0].evidence, 0.6))
    for tid, hit in match_terms(HANDOFF_TERMS, _segments(texts)).items():
        hits.append(StageHit("handoff", f"text:{tid}", _first_hit_ev(hit), 0.7))
    if fields:
        hits.append(StageHit("capture", "fields:" + ",".join(sorted(fields)), EvidencePointer(
            note="union of captured field types"), 0.9))


@register("nar_funnel", family=Family.NAR, version=VERSION)
def nar_funnel(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """Observed scam kill-chain stages: lure > trust > offer > capture > handoff > payment.

    `funnel_stage` genes (one per observed stage, evidence = strongest signal) and one `funnel_sequence`
    gene (stages in canonical order). dependency_group `nar:mo` (modus operandi): group-level similarity and
    explanation only; NAR adds 0 bits to campaign links. A stage that is not observed is unknown (the page may
    be missing or a white page), never evidence of absence."""
    ex = "nar_funnel"
    texts = collect(inc, ctx)
    base_genes = nar(inc, ctx).genes
    hits = funnel_hits(inc, ctx, texts, base_genes)
    if not hits:
        return ExtractResult()
    genes: list[Gene] = []
    present = []
    for stage in FUNNEL_STAGES:
        sh = sorted([h for h in hits if h.stage == stage], key=lambda h: -h.quality)
        if not sh:
            continue
        present.append(stage)
        signals = ", ".join(dict.fromkeys(h.signal for h in sh))[:280]
        ev = sh[0].evidence.model_copy(update={"note": f"{sh[0].signal}; signals: {signals}"[:300]})
        genes.append(Gene(incident_id=inc.incident.id, family=Family.NAR, kind="funnel_stage", value=stage,
                          quality=round(sh[0].quality, 3), provenance=Provenance.INF, evidence=ev,
                          extractor=f"{ex}@{VERSION}", dependency_group="nar:mo"))
        if stage == "capture":
            fs = next((h.signal for h in sh if h.signal.startswith("fields:")), None)
            if fs:
                genes.append(Gene(incident_id=inc.incident.id, family=Family.NAR, kind="funnel_capture_fields",
                                  value=fs.split(":", 1)[1], quality=0.9, provenance=Provenance.INF,
                                  evidence=ev, extractor=f"{ex}@{VERSION}", dependency_group="nar:mo"))
    has_landing = bool(inc.by_role(ArtifactRole.LANDING_HTML))
    genes.append(Gene(incident_id=inc.incident.id, family=Family.NAR, kind="funnel_sequence", value=">".join(present),
                      quality=0.8 if has_landing else 0.4, provenance=Provenance.INF,
                      evidence=EvidencePointer(note=("landing page present" if has_landing else
                                                     "no landing page: later stages unobserved") +
                                               f"; {len(present)}/{len(FUNNEL_STAGES)} stages"),
                      extractor=f"{ex}@{VERSION}", dependency_group="nar:mo"))
    return ExtractResult(genes=genes)
