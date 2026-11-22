"""TXT family extractors: OCR lines, headlines, phrase shingles, MinHash, char n-gram signature, text
embeddings, entities, obfuscation, language.

Design rules (PLAN_MAX §1, SPEC_ERRATA):
- discrete text genes carry the folded key as `value` (lowercase, no diacritics, single spaces) so OCR case
  and diacritic noise does not break exact matching; the observed text goes to `evidence.note`;
- embeddings use light normalisation only; they are evidence for the scorer, never a linking rule;
- MinHash / phrases use aggressive normalisation; each MinHash LSH band and each sampled phrase is a
  discrete gene, so rarity (document frequency) decides how much a shared phrase is worth;
- every gene group shares a `dependency_group` so the scorer does not sum correlated genes per token.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome import adv_f01
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, Vector, register
from hydra.genome.lexicons import MEDIA_BRANDS, compile_terms
from hydra.genome.operator_ids import find_intl_phones, find_operator_ids, tier_phone
from hydra.genome.txt_entities import (
    Entity,
    find_amounts,
    find_brands,
    find_emails,
    find_hosts,
    find_persons,
    find_phones,
    find_products,
)
from hydra.genome.txt_norm import detect_obfuscation, fold, guess_language, tokens
from hydra.genome.txt_sources import IncidentTexts, Segment, TextBlock, collect
from hydra.genome.webparse import pii_hash

log = logging.getLogger(__name__)

VERSION = 1
# 2: operator ids (accounts + bank code, wallets, messengers), phone tiers; 3: refanged IOCs and toll-fraud chain ids
# txt_entities 4: IP-like dotted numbers are not phones
# txt_phrases 2: `ocr5:` MinHash of screenshot/creative OCR with the page shingling (cross-modal text, E-XM-1)
# txt_entities 5 (feat/e2e-fix): phone-number chat senders; 6: both rules merged (merge-batch5)
EXTRACTOR_VERSION = {"txt_entities": 6, "txt_phrases": 2}
# ADV-F01 (HYDRA_ADV_F01=1): txt_entities 7, txt_lines 2 (hydra/genome/adv_f01.py); unchanged without the flag
if adv_f01.enabled():
    EXTRACTOR_VERSION.update({"txt_entities": adv_f01.version("txt_entities", 6),
                              "txt_lines": adv_f01.version("txt_lines", VERSION)})
NOISE_ZONES = {"feed", "library_panel"}
PAGE_SOURCES_FOR_PHRASES = {"html_title", "html_headline", "html_lead", "html_body", "html_nav", "html_comment",
                            "html_form", "html_alt", "html_byline"}
MAX_OCR_LINES = 400
MAX_PHRASES = 300
MAX_PER_ENTITY_KIND = 60
MINHASH_PERM = 128
MINHASH_BANDS = 32
CHARSIG_DIM = 1024


def key(text: str) -> str:
    """Folded matching key: lowercase, no diacritics, punctuation removed, single spaces."""
    return " ".join(fold(text).split())


# dependency_group = "<family>:<tier>:<name>" (CORE synapse convention, INFRA_GENES.md): operator / phrase =
# campaign-level, platform / kit = kit-level (capped towards campaign links), persona / asset = asset-level,
# boilerplate = 0 bits.
TIER_BY_KIND = {
    "TXT": {"language": "boilerplate", "brand_mention": "boilerplate", "commenter_name": "boilerplate",
            "lang_mismatch": "kit", "obfuscation": "kit", "ui_widget": "kit", "entity_discount": "kit",
            "entity_phone": "operator", "entity_email": "operator", "entity_iban": "operator",
            "domain_in_text": "operator", "url_in_text": "operator", "promo_code": "operator",
            "product_name": "operator", "person_named": "persona", "text": "boilerplate",
            "bank_code": "platform", "crypto_wallet": "operator", "messenger_handle": "operator",
            "payment_method": "kit", "text_template_key": "phrase",
            # toll-fraud chain ids (hydra/genome/tollfraud.py; finders set the tier explicitly as well)
            "app_package_id": "operator", "app_developer_id": "operator", "platform_ad_id": "operator",
            "platform_page_id": "operator", "premium_sms": "operator", "premium_sms_code": "platform",
            "carrier_billing_marker": "kit", "cloud_lb_host": "platform", "cloud_lb_name": "operator",
            "tls_cert_sha256": "operator"},
    "NAR": {},  # narrative genes describe the scheme family: kit level ("similar campaigns")
    "META": {"platform_ad_id": "operator", "platform_page_id": "operator", "platform_page_name": "operator",
             "capture_date": "platform", "ad_start_date": "platform", "upload_date": "platform",
             "release_date": "platform", "upload_time": "platform"},
}
DEFAULT_TIER = {"TXT": "phrase", "NAR": "kit", "META": "boilerplate"}


def dep_group(family: str, kind: str, hint: str | None, tier: str | None = None) -> str:
    tier = tier or TIER_BY_KIND.get(family, {}).get(kind, DEFAULT_TIER.get(family, "phrase"))
    name = hint or kind
    for pre in ("txt_", "nar_", "meta_"):
        name = name.removeprefix(pre)
    return f"{family}:{tier}:{name}"


def _gene(inc: ResolvedIncident, ex: str, kind: str, value: str | None, ev: EvidencePointer | None = None,
          quality: float = 1.0, group: str | None = None, prov: Provenance = Provenance.OBS,
          vector_ref: str | None = None, status: GeneStatus = GeneStatus.OBSERVED,
          family: Family = Family.TXT, tier: str | None = None) -> Gene:
    return Gene(incident_id=inc.incident.id, family=family, kind=kind, value=value, vector_ref=vector_ref,
                quality=round(max(0.0, min(1.0, quality)), 3), status=status, provenance=prov,
                evidence=ev or EvidencePointer(), extractor=f"{ex}@{EXTRACTOR_VERSION.get(ex, VERSION)}",
                dependency_group=dep_group(str(family), kind, group, tier))


def _ev(seg: Segment, start: int | None = None, end: int | None = None, note: str | None = None) -> EvidencePointer:
    upd: dict = {}
    if start is not None and end is not None:
        upd["char_range"] = (start, end)
    if note is not None:
        upd["note"] = note[:300]
    return seg.evidence.model_copy(update=upd)


def dedupe(genes: Iterable[Gene]) -> list[Gene]:
    """One gene per (kind, value) per incident: keep the highest quality, first seen on ties."""
    best: dict[tuple[str, str | None], Gene] = {}
    order: list[tuple[str, str | None]] = []
    for g in genes:
        k = (g.kind, g.value if g.value is not None else g.vector_ref)
        if k not in best:
            best[k] = g
            order.append(k)
        elif g.quality > best[k].quality:
            best[k] = g
    return [best[k] for k in order]


def content_segments(texts: IncidentTexts, channels: tuple[str, ...]) -> list[tuple[TextBlock, Segment]]:
    out = []
    for b in texts.blocks:
        if b.channel not in channels or b.source in ("html_meta", "html_commenter"):
            continue
        for s in b.segments:
            if s.chrome or s.zone in NOISE_ZONES or not s.text:
                continue
            out.append((b, s))
    return out


def _status_genes(inc: ResolvedIncident, texts: IncidentTexts, ex: str) -> list[Gene]:
    genes = [_gene(inc, ex, "text", None, EvidencePointer(artifact_id=aid, note=msg), status=GeneStatus.FAILED)
             for aid, msg in texts.errors]
    genes += [_gene(inc, ex, "text", None, EvidencePointer(artifact_id=aid, note=msg), status=GeneStatus.UNAVAILABLE)
              for aid, msg in texts.unavailable]  # missing OCR weights = unknown observation (CR-ASTRA-PIPE-101)
    if not texts.blocks:
        st = GeneStatus.UNAVAILABLE if texts.attempted else GeneStatus.NOT_APPLICABLE
        genes.append(_gene(inc, ex, "text", None, EvidencePointer(note="no text found in any artifact"),
                           status=st))
    return genes


# =============================================================================== txt_lines


def ad_headline(texts: IncidentTexts) -> tuple[Segment, float] | None:
    """Largest-glyph OCR line with >= 3 words in the ad zone of creatives/screenshots (quality 0.6)."""
    best: tuple[float, Segment] | None = None
    for b in texts.by_source("ocr_image"):
        if b.channel != "ad":
            continue
        for s in b.segments:
            if s.chrome or s.zone != "ad" or sum(t.text.isalpha() for t in tokens(s.text)) < 3:
                continue
            score = s.height * (0.5 + 0.5 * s.quality)
            if best is None or score > best[0]:
                best = (score, s)
    return (best[1], 0.6) if best else None


def page_headline(texts: IncidentTexts) -> tuple[Segment, float] | None:
    for src, q in (("html_headline", 1.0), ("html_title", 0.8)):
        for b in texts.by_source(src):
            for s in b.segments:
                if len(tokens(s.text)) >= 2:
                    return s, q
    for b in texts.by_source("html_meta"):
        for s in b.segments:
            if s.meta.get("key") in ("og:title", "twitter:title"):
                return s, 0.8
    return None


_BYLINE_INLINE = re.compile(
    r"\b(?:oprac\.?|autor(?:ka)?:?|tekst:?|fot\.)\s+([A-ZĄĆĘŁŃÓŚŹŻ][\w-]+\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w-]+)"
)


@register("txt_lines", family=Family.TXT, version=EXTRACTOR_VERSION.get("txt_lines", VERSION))
def txt_lines(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """OCR lines with boxes, page title, headline, lead, byline, language, obfuscation, brand mentions."""
    ex = "txt_lines"
    texts = collect(inc, ctx)
    genes = _status_genes(inc, texts, ex)
    # OCR lines (ad, page screenshots, landing images, Ad Library ad copy)
    n = 0
    for b in texts.blocks:
        if not b.source.startswith("ocr"):
            continue
        for s in b.segments:
            if s.chrome or s.zone in NOISE_ZONES or len(s.text) < 6 or len(tokens(s.text)) < 2:
                continue
            if n >= MAX_OCR_LINES:
                break
            kind = "library_ad_text" if s.zone == "library_ad" else "ocr_line"
            genes.append(_gene(inc, ex, kind, key(s.text), _ev(s, note=s.text), s.quality * 0.9, "txt_ocr"))
            n += 1
    # titles / headlines / leads
    for b in texts.by_source("html_title"):
        for s in b.segments:
            genes.append(_gene(inc, ex, "page_title", key(s.text), _ev(s, note=s.text), 1.0, "txt_headline"))
    ph = page_headline(texts)
    if ph:
        genes.append(_gene(inc, ex, "headline", key(ph[0].text), _ev(ph[0], note=ph[0].text), ph[1], "txt_headline"))
    ah = ad_headline(texts)
    if ah:
        genes.append(_gene(inc, ex, "ad_headline", key(ah[0].text), _ev(ah[0], note=ah[0].text), ah[1] *
                           ah[0].quality, "txt_headline"))
    for b in texts.by_source("html_lead"):
        for s in b.segments[:1]:
            genes.append(_gene(inc, ex, "lead", key(s.text)[:300], _ev(s, note=s.text), s.quality, "txt_headline"))
    for b in texts.by_source("html_meta"):
        for s in b.segments:
            k = s.meta.get("key", "")
            if k in ("description", "og:description", "og:site_name", "author", "keywords"):
                genes.append(_gene(inc, ex, f"meta_{k.replace('og:', 'og_')}", key(s.text)[:300],
                                   _ev(s, note=s.text), 1.0, "txt_headline"))
    # bylines
    for b in texts.by_source("html_byline"):
        for s in b.segments:
            if sum(len(w) > 2 for w in key(s.text).split()) >= 2:
                genes.append(_gene(inc, ex, "byline", key(s.text), _ev(s, note=s.text), 0.9, "txt_byline"))
    for b in texts.by_source("html_body", "html_lead"):
        for s in b.segments:
            for m in _BYLINE_INLINE.finditer(s.text):
                genes.append(_gene(inc, ex, "byline", key(m.group(1)), _ev(s, m.start(1), m.end(1), m.group(0)),
                                   0.8, "txt_byline"))
    # language per channel + declared html lang
    for ch in ("ad", "page", "text"):
        segs = content_segments(texts, (ch,))
        if not segs:
            continue
        lang, conf = guess_language(" ".join(s.text for _, s in segs)[:20000])
        if lang:
            genes.append(_gene(inc, ex, "language", lang, EvidencePointer(artifact_id=segs[0][1].evidence.artifact_id,
                                                                          note=f"channel={ch}"), conf, "txt_lang",
                               Provenance.INF))
            if ch == "page":
                declared = next((b.meta.get("declared_lang") for b in texts.by_channel("page")
                                 if b.meta.get("declared_lang")), None)
                if declared and declared.split("-")[0].lower() != lang:
                    genes.append(_gene(inc, ex, "lang_mismatch", f"{declared.split('-')[0].lower()}->{lang}",
                                       EvidencePointer(artifact_id=segs[0][1].evidence.artifact_id,
                                                       note=f'<html lang="{declared}"> but text reads as {lang}'),
                                       conf, "txt_lang", Provenance.INF))
    # obfuscation (raw text)
    found: dict[str, tuple[float, EvidencePointer, int]] = {}
    for b in texts.blocks:
        is_ocr = b.source.startswith("ocr")
        for s in b.segments:
            if s.zone in NOISE_ZONES:
                continue
            for o in detect_obfuscation(s.raw, ocr=is_ocr):
                q = 0.4 if is_ocr else 0.9
                prev = found.get(o.technique)
                cnt = o.count + (prev[2] if prev else 0)
                if prev is None or q > prev[0]:
                    found[o.technique] = (q, _ev(s, note=f"example: {o.example!r}"), cnt)
                else:
                    found[o.technique] = (prev[0], prev[1], cnt)
    for tech, (q, ev, cnt) in found.items():
        ev = ev.model_copy(update={"note": f"{ev.note}; occurrences: {cnt}"})
        genes.append(_gene(inc, ex, "obfuscation", tech, ev, q, "txt_obfuscation", Provenance.INF))
    # brand mentions (media / institutions) with bbox when from OCR
    for b in texts.blocks:
        if b.source in ("html_commenter",):
            continue
        for s in b.segments:
            if s.zone in NOISE_ZONES:
                continue
            for e in find_brands(s.text):
                if e.kind != "brand_mention":
                    continue
                genes.append(_gene(inc, ex, "brand_mention", e.value, _ev(s, e.start, e.end, s.text[e.start:e.end]),
                                   e.quality * (s.quality if b.source.startswith("ocr") else 1.0), "txt_brand"))
    for b in texts.by_channel("page"):
        for s in b.segments:
            w = ui_widget(s)
            if w:
                genes.append(_gene(inc, ex, "ui_widget", w, _ev(s, note=s.text[:120]), 0.8, "txt_widget"))
    return ExtractResult(genes=dedupe(genes))


# =============================================================================== txt_phrases


def shingles(toks: list[str], k: int) -> list[str]:
    if len(toks) < k:
        return [" ".join(toks)] if len(toks) >= 2 else []
    return [" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)]


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


def _informative(sh: str) -> bool:
    parts = sh.split()
    return sum(p.isdigit() for p in parts) * 2 < len(parts) and sum(len(p) for p in parts) >= 3 * len(parts)


def minhash(shs: list[str]):
    from datasketch import MinHash

    m = MinHash(num_perm=MINHASH_PERM, seed=1)
    m.update_batch([s.encode() for s in shs])
    return m


def minhash_bands(hv: np.ndarray, bands: int = MINHASH_BANDS) -> list[str]:
    rows = len(hv) // bands
    return [hashlib.blake2b(hv[i * rows:(i + 1) * rows].astype("<u8").tobytes(), digest_size=6).hexdigest()
            for i in range(bands)]


def char_signature(text: str) -> np.ndarray | None:
    """Char 3-5-gram TF signature (sublinear tf, feature hashing to 1024 dims, L2). Cosine of two
    signatures approximates char-n-gram cosine; IDF is applied by the scorer's background model."""
    from sklearn.feature_extraction.text import HashingVectorizer

    t = key(text)
    if len(t) < 20:
        return None
    hv = HashingVectorizer(analyzer="char_wb", ngram_range=(3, 5), n_features=CHARSIG_DIM, alternate_sign=True,
                           norm=None, lowercase=False)
    x = hv.transform([t]).toarray()[0]
    x = np.sign(x) * np.log1p(np.abs(x))
    n = np.linalg.norm(x)
    return (x / n).astype(np.float32) if n > 0 else None


_COUNTRIES = frozenset("afghanistan albania algeria andorra angola argentina armenia australia austria azerbaijan "
                       "bahamas bahrain bangladesh belarus belgium brazil bulgaria canada chile china colombia "
                       "croatia cyprus czechia denmark egypt estonia finland france georgia germany greece hungary "
                       "iceland india indonesia iran iraq ireland israel italy jamaica japan jordan kazakhstan kenya "
                       "latvia lithuania mexico moldova netherlands norway pakistan poland portugal romania russia "
                       "serbia slovakia slovenia spain sweden switzerland turkey ukraine".split())


def ui_widget(seg: Segment) -> str | None:
    """Text of embedded UI widgets (phone country-code pickers of lead-form SDKs). Shared by every page using
    the same SDK: kit-level evidence, never phrase evidence."""
    words = set(re.findall(r"[a-z]+", fold(seg.text)))
    if len(words & _COUNTRIES) >= 8:
        return "country_dial_picker"
    return None


def channel_tokens(texts: IncidentTexts) -> dict[str, list[tuple[Segment, list]]]:
    """Token streams per channel: ad (+ asset OCR, library ad copy), page, text."""
    chans: dict[str, list[tuple[Segment, list]]] = {}
    for b in texts.blocks:
        if b.source in ("html_meta", "html_commenter"):
            continue
        if b.channel == "page" and b.source.startswith("html") and b.source not in PAGE_SOURCES_FOR_PHRASES:
            continue
        for s in b.segments:
            if s.chrome or s.zone in NOISE_ZONES or not s.text or ui_widget(s):
                continue
            ch = "library_ad" if s.zone == "library_ad" else ("ad" if b.channel in ("ad", "meta") else b.channel)
            toks = tokens(s.text)
            if toks:
                chans.setdefault(ch, []).append((s, toks))
    return chans


# Consent / legal boilerplate lines (cookie banners, privacy, rights notices, consent buttons) printed on most pages
# of any operator. Dropped from the cross-modal OCR stream only: they would raise the containment of a screenshot in
# an unrelated page that carries the same banner (CR-OPGENES-12).
_OCR_BOILERPLATE = re.compile(
    r"\b(cookie\w*|ciasteczk\w*|polityk\w* prywatnosci|privacy policy|rodo|gdpr|przetwarzani\w* (twoich )?danych|"
    r"administrator\w* danych|wszelkie prawa zastrzezone|all rights reserved|prawa autorskie|copyright|"
    r"regulamin\w*|terms of (use|service)|akceptuj\w*|zaakceptuj\w*|accept all|zgadzam sie|ustawienia prywatnosci|"
    r"partner\w* reklamow\w*|newsletter\w*)\b")
OCR5_SOURCES = ("ocr_image", "ocr_frame")


def ocr_page_tokens(texts: IncidentTexts) -> list[str]:
    """Folded word tokens of screenshot / creative OCR (channel `ad`: creatives, feed and landing screenshots that
    are not role `landing_screenshot`, video frames) for the cross-modal page-text signature (`ocr5:`).

    Same filters as `channel_tokens` (platform chrome, feed / Ad Library panel zones, UI widgets), plus consent and
    legal boilerplate lines. Landing-asset OCR (`asset`, images inside a saved page) is excluded: it is part of the
    saved page, not a rendering of it. OCR of role `landing_screenshot` is already in the page channel."""
    out: list[str] = []
    for b in texts.blocks:
        if b.channel != "ad" or b.source not in OCR5_SOURCES:
            continue
        for s in b.segments:
            if s.chrome or s.zone in NOISE_ZONES or not s.text or ui_widget(s) or _OCR_BOILERPLATE.search(key(s.text)):
                continue
            out.extend(t.text for t in tokens(s.text))
    return out


def _minhash_genes(inc: ResolvedIncident, ex: str, ch: str, mshs: list[str], aid: str | None, group: str,
                   bands: bool = True, tier: str | None = None) -> list[Gene]:
    """`minhash_sig` "<ch>:<hex>" (evidence note carries `shingles=N`) + 32 LSH bands."""
    try:
        hv = minhash(mshs).hashvalues
    except Exception as exc:  # noqa: BLE001
        return [_gene(inc, ex, "minhash_sig", None, EvidencePointer(note=str(exc)[:200]), status=GeneStatus.FAILED)]
    note = f"channel={ch}; shingles={len(mshs)}; perm={MINHASH_PERM}"
    genes = [_gene(inc, ex, "minhash_sig", f"{ch}:" + hv.astype(">u4").tobytes().hex(),
                   EvidencePointer(artifact_id=aid, note=note), 1.0, group, Provenance.INF, tier=tier)]
    for bi, bh in enumerate(minhash_bands(hv) if bands else ()):
        genes.append(_gene(inc, ex, "minhash_band", f"{ch}:{bi:02d}:{bh}", EvidencePointer(artifact_id=aid, note=note),
                           1.0, group, Provenance.INF))
    return genes


@register("txt_phrases", family=Family.TXT, version=EXTRACTOR_VERSION["txt_phrases"])
def txt_phrases(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """Sampled phrase shingles (readable, rare-phrase matching), MinHash signature + LSH bands, char n-gram
    signature vectors, per channel."""
    ex = "txt_phrases"
    texts = collect(inc, ctx)
    genes: list[Gene] = []
    vectors: list[Vector] = []
    for ch, items in channel_tokens(texts).items():
        k = 5 if ch == "page" else 4
        mod = 8 if ch == "page" else 1
        # phrases: shingles within a segment stream; evidence = segment where the shingle starts
        flat: list[tuple[str, Segment, int, int]] = []
        for s, toks in items:
            for t in toks:
                flat.append((t.text, s, t.start, t.end))
        words = [w for w, *_ in flat]
        shs = shingles(words, k)
        picked: dict[str, int] = {}
        for i, sh in enumerate(shs):
            if not _informative(sh):
                continue
            h = _h64(sh)
            if h % mod == 0 and sh not in picked:
                picked[sh] = i
        ranked = sorted(picked.items(), key=lambda kv: _h64(kv[0]))[:MAX_PHRASES]
        for sh, i in ranked:
            _, s, st, _ = flat[i]
            end_seg = flat[min(i + k - 1, len(flat) - 1)]
            en = end_seg[3] if end_seg[1] is s else len(s.text)
            genes.append(_gene(inc, ex, "phrase", sh, _ev(s, st, en, s.text[st:en]), s.quality, f"txt_phrase_{ch}"))
        first_aid = items[0][0].evidence.artifact_id
        # MinHash (k=5 page, k=3 short channels) over all shingles of the channel
        mshs = shingles(words, 5 if ch == "page" else 3)
        if len(mshs) >= 3:
            genes += _minhash_genes(inc, ex, ch, mshs, first_aid, f"txt_minhash_{ch}")
        sig = char_signature(" ".join(s.text for s, _ in items))
        if sig is not None:
            vk = f"{inc.incident.id}|{first_aid}|charsig_{ch}|0"
            vectors.append(Vector(vk, f"char35-hash{CHARSIG_DIM}@{VERSION}", sig))
            genes.append(_gene(inc, ex, f"charsig_{ch}", f"char35-hash{CHARSIG_DIM}@{VERSION}",
                               EvidencePointer(artifact_id=first_aid, note=f"channel={ch}"), 1.0,
                               f"txt_charsig_{ch}", Provenance.INF, vector_ref=vk))
    # cross-modal: screenshot / creative OCR shingled like the page channel (word 5-shingles, same permutations),
    # so `hydra.crossmodal` can estimate how much of the OCR text is contained in a saved page of another incident
    ocr_toks = ocr_page_tokens(texts)
    ocr_shs = shingles(ocr_toks, 5) if len(ocr_toks) >= 5 else []
    if len(ocr_shs) >= 3:
        aid = next((b.artifact_id for b in texts.blocks if b.channel == "ad" and b.source in OCR5_SOURCES), None)
        # tier `label`: never scored as a shared discrete value (an identical creative already matches through the
        # `ad:` signature and its bands); no bands either, they would only re-match `ocr5:` with `ocr5:`
        genes += _minhash_genes(inc, ex, "ocr5", ocr_shs, aid, "txt_minhash_ocr5", bands=False, tier="label")
    return ExtractResult(genes=dedupe(genes), vectors=vectors)


# =============================================================================== txt_entities


def _entity_gene(inc, ex, e: Entity, seg: Segment, block: TextBlock, qmul: float = 1.0) -> Gene:
    group = {"entity_phone": "txt_contact", "entity_email": "txt_contact", "entity_iban": "txt_contact",
             "domain_in_text": "txt_domain", "url_in_text": "txt_domain", "entity_price": "txt_price",
             "entity_discount": "txt_price", "promo_code": "txt_promo", "person_named": "txt_person",
             "product_name": "txt_product", "commenter_name": "txt_commenter", "bank_code": "bank",
             "crypto_wallet": "crypto"}.get(e.kind)
    group = e.group or group
    prov = Provenance.INF if e.quality < 0.7 else Provenance.OBS
    q = e.quality * qmul * (seg.quality if block.source.startswith("ocr") else 1.0)
    shown = seg.text[e.start:e.end]
    value = e.value
    if e.kind == "entity_iban":  # account numbers: salted hash only (same value as inf_dom), masked note
        value, shown = pii_hash(e.value), ""
    note = shown + (f" ({e.note})" if e.note else "")
    return _gene(inc, ex, e.kind, value, _ev(seg, e.start, e.end, note.strip()), q, group, prov, tier=e.tier)


# GAPS_PAPERS G02: "same text, other identifiers". Identifiers are replaced by typed masks before folding, so a
# re-posted ad with a new URL, phone and price keeps one key. Phrase tier: the TXT cap still applies, so the key
# never links alone.
TEMPLATE_CHANNELS = {"ad", "text"}
TEMPLATE_SOURCES = {"html_title", "html_headline", "html_lead", "html_form", "meta_title", "meta_description", "asr"}
TEMPLATE_MIN_WORDS = 6
_MASK = {"entity_phone": "qtelq", "entity_email": "qmailq", "entity_iban": "qkontoq", "bank_code": "qkontoq",
         "crypto_wallet": "qportfelq", "messenger_handle": "qkontaktq", "domain_in_text": "qurlq",
         "url_in_text": "qurlq",
         "entity_price": "qkwotaq", "entity_discount": "qrabatq", "promo_code": "qkodq"}
_DATE_TIME = re.compile(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b")


def template_key(text: str, ents: Iterable[Entity]) -> tuple[str, str] | None:
    """(key, masked folded text) with identifiers, dates, times and numbers masked; None when fewer than
    TEMPLATE_MIN_WORDS words remain or nothing was masked (the plain text key already covers that case)."""
    spans = sorted(((e.start, e.end, _MASK[e.kind]) for e in ents if e.kind in _MASK and e.end > e.start),
                   key=lambda x: (x[0], -x[1]))
    out, pos, masks = [], 0, 0
    for st, en, tok in spans:
        if st < pos:
            continue
        out.append(text[pos:st])
        out.append(f" {tok} ")
        pos, masks = en, masks + 1
    out.append(text[pos:])
    masked = _DATE_TIME.sub(" qdataq ", "".join(out))
    words = []
    for w in key(masked).split():
        if w.isdigit():
            w = "qnumq"
        if w.startswith("q") and w.endswith("q") and len(w) > 2:
            masks += 1
        words.append(w)
    plain = [w for w in words if not (w.startswith("q") and w.endswith("q") and len(w) > 2)]
    if masks == 0 or len(plain) < TEMPLATE_MIN_WORDS:
        return None
    folded = " ".join(words)
    return hashlib.sha256(folded.encode()).hexdigest()[:16], folded[:200]


_PHONE_SENDER = re.compile(r"[+\d\s()-]{7,}")


def _chat_sender_phones(inc: ResolvedIncident, ex: str, texts: IncidentTexts) -> list[Gene]:
    """CR-E2E-1: a chat export keeps the sender out of the message text (`[01.10.2026, 10:00] +48 733 112 909: ...`),
    and `txt_chat` leaves a phone-number sender to this extractor; the sender is scanned once per distinct number."""
    out: list[Gene] = []
    seen: set[str] = set()
    for b in texts.blocks:
        for s in b.segments:
            snd = str((s.meta or {}).get("sender") or "").strip()
            if not snd or snd in seen or not _PHONE_SENDER.fullmatch(snd):
                continue
            seen.add(snd)
            seg = Segment(snd, snd, EvidencePointer(artifact_id=s.evidence.artifact_id,
                                                    note=f"chat sender; {s.evidence.note or ''}".strip()[:200]),
                          meta={"sender": snd})
            out += [_entity_gene(inc, ex, tier_phone(e), seg, b) for e in find_phones(snd)]
            out += [_entity_gene(inc, ex, e, seg, b) for e in find_intl_phones(snd)]
    return out


@register("txt_entities", family=Family.TXT, version=EXTRACTOR_VERSION["txt_entities"])
def txt_entities(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """Phones, IBANs, emails, domains/URLs in text, prices, discounts, promo codes, persons, products."""
    ex = "txt_entities"
    texts = collect(inc, ctx)
    genes: list[Gene] = []
    persons_seen: set[str] = set()
    for b in texts.blocks:
        if b.source == "html_commenter":
            for s in b.segments:
                genes.append(_gene(inc, ex, "commenter_name", key(s.text), _ev(s, note=s.text), 0.2, "txt_commenter",
                                   Provenance.OBS))
            continue
        for s in b.segments:
            t = s.text
            if not t:
                continue
            noise = s.zone in NOISE_ZONES
            hosts = find_hosts(t)
            for e in hosts:
                genes.append(_entity_gene(inc, ex, e, s, b, 0.7 if noise else 1.0))
            if noise:
                continue
            found: list[Entity] = list(hosts)
            for e in find_phones(t):
                found.append(e)
                genes.append(_entity_gene(inc, ex, tier_phone(e), s, b))
            for finder in (find_intl_phones, find_operator_ids, find_emails, find_amounts):
                for e in finder(t):
                    found.append(e)
                    genes.append(_entity_gene(inc, ex, e, s, b))
            if b.channel in TEMPLATE_CHANNELS or b.source in TEMPLATE_SOURCES:
                tk = template_key(t, found)
                if tk is not None:
                    genes.append(_gene(inc, ex, "text_template_key", tk[0], _ev(s, note=tk[1]), 0.7,
                                       "txt_template", Provenance.INF))
            for e in find_persons(t):
                if b.source == "html_comment":
                    # names inside fake comment threads are commenter personas, not discriminative
                    genes.append(_entity_gene(inc, ex, Entity("commenter_name", key(e.value), e.start, e.end, 0.2,
                                                              e.note), s, b))
                    continue
                persons_seen.add(e.value)
                genes.append(_entity_gene(inc, ex, e, s, b))
            for e in find_brands(t):
                if e.kind == "product_name":
                    genes.append(_entity_gene(inc, ex, e, s, b))
    genes += _chat_sender_phones(inc, ex, texts)
    # context-introduced product names (second pass: needs the person list)
    for b in texts.blocks:
        if b.source in ("html_commenter", "html_comment", "html_nav"):
            continue
        for s in b.segments:
            if s.zone in NOISE_ZONES or s.chrome:
                continue
            for e in find_products(s.text, persons_seen):
                genes.append(_entity_gene(inc, ex, e, s, b))
    # short page titles of product landings (e.g. "Kardiovit Forte", two capitalised words)
    media = compile_terms(MEDIA_BRANDS)
    for b in texts.by_source("html_title"):
        for s in b.segments:
            k = key(s.text)
            if 1 <= len(k.split()) <= 4 and not any(rx.search(k) for _, rx in media) and len(k) >= 4:
                genes.append(_gene(inc, ex, "product_name", k, _ev(s, note=f"short page title: {s.text}"), 0.7,
                                   "txt_product", Provenance.INF))
    # cap noisy kinds
    out, counts = [], {}
    for g in dedupe(genes):
        counts[g.kind] = counts.get(g.kind, 0) + 1
        if counts[g.kind] <= MAX_PER_ENTITY_KIND:
            out.append(g)
    return ExtractResult(genes=out)


# =============================================================================== txt_embed

EMB_MAX_CHARS = 1800


def _embedder(ctx: ExtractContext):
    name = ctx.options.get("text_embedder", "e5-small")
    cache_key = f"text_embedder:{name}"
    if cache_key not in ctx.models:
        from hydra.ml.models import load_text_embedder, model_dir

        model_dir(name, ctx.settings)  # honour ctx.settings.models_dir (raises ModelUnavailable)
        ctx.models[cache_key] = load_text_embedder(name)
    return ctx.models[cache_key]


def embedding_inputs(texts: IncidentTexts) -> list[tuple[str, str, str]]:
    """(kind, artifact_id, text) for headline, body and ad text."""
    out = []
    ph = page_headline(texts)
    ah = ad_headline(texts)
    if ph:
        out.append(("emb_headline", ph[0].evidence.artifact_id or "-", ph[0].text))
    elif ah:
        out.append(("emb_headline", ah[0].evidence.artifact_id or "-", ah[0].text))
    body = [s for b in texts.by_source("html_lead", "html_body") for s in b.segments if len(s.text) > 20]
    if body:
        out.append(("emb_body", body[0].evidence.artifact_id or "-", " ".join(s.text for s in body)[:EMB_MAX_CHARS]))
    ad = [s for _, s in content_segments(texts, ("ad",))]
    if ad:
        txt = " ".join(dict.fromkeys(s.text for s in ad))
        if len(tokens(txt)) >= 3:
            out.append(("emb_ad", ad[0].evidence.artifact_id or "-", txt[:EMB_MAX_CHARS]))
    plain = [s for b in texts.by_source("plain_text") for s in b.segments]
    if plain:
        out.append(("emb_text", plain[0].evidence.artifact_id or "-", " ".join(s.text for s in plain)[:EMB_MAX_CHARS]))
    return out


@register("txt_embed", family=Family.TXT, version=VERSION)
def txt_embed(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """Text embeddings (multilingual-e5-small by default; symmetric 'query:' prefix) for headline, body,
    ad text. Vectors are evidence for the scorer (background-normalised bits), never a link rule."""
    ex = "txt_embed"
    texts = collect(inc, ctx)
    inputs = embedding_inputs(texts)
    if not inputs:
        return ExtractResult()
    try:
        emb = _embedder(ctx)
    except Exception as exc:  # noqa: BLE001 — ModelUnavailable or broken install
        note = f"embedder unavailable: {exc}"[:300]
        return ExtractResult(genes=[_gene(inc, ex, kind, None, EvidencePointer(note=note),
                                          status=GeneStatus.UNAVAILABLE) for kind, _, _ in inputs])
    vecs = emb.embed([t for _, _, t in inputs], kind="query")
    model = f"{emb.key}"
    genes, vectors = [], []
    for (kind, aid, text), v in zip(inputs, vecs, strict=True):
        vk = f"{inc.incident.id}|{aid}|{kind}|0"
        vectors.append(Vector(vk, model, np.asarray(v, dtype=np.float32)))
        genes.append(_gene(inc, ex, kind, model, EvidencePointer(artifact_id=None if aid == "-" else aid,
                                                                 note=text[:200]), 1.0, f"txt_{kind}",
                           Provenance.INF, vector_ref=vk))
    return ExtractResult(genes=genes, vectors=vectors)


from hydra.genome import txt_to_inf as _txt_to_inf  # noqa: E402,F401  (registers inf_text_hosts)
