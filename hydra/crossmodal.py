"""Cross-modal page-text near-duplicate (CR-OPGENES-12, SPEC_ERRATA E-XM-1).

A full-page screenshot of a known landing page only yields OCR text. Its phrase genes (word 4-shingles, channel
`ad`) never equal the saved page's phrases (word 5-shingles sampled 1/8), and the page-vs-page rule
(`synapse._text_near_duplicate`, Jaccard >= 0.9) cannot fire because the screenshot shows only part of the page.

`txt_phrases` therefore also emits `minhash_sig` "ocr5:<hex>" for screenshot / creative OCR, shingled exactly like
the page channel (word 5-shingles over `fold()` tokens, the same 128 permutations). This module estimates the
containment of the OCR shingle set in the other incident's page shingle set from the MinHash Jaccard J and the two
set sizes: |O ∩ P| = J (|O| + |P|) / (1 + J), containment = |O ∩ P| / |O|.

The rule is its own dependency group (`TXT:asset:cross_modal`, near-duplicate kind, outside the 8-bit TXT campaign
cap), like the page-vs-page near-duplicate. Guards:
- the OCR side has no saved page of its own (two saved pages are compared by the page-vs-page rule);
- >= XM_MIN_OCR_SHINGLES OCR shingles after the extractor dropped platform chrome, feed / Ad Library zones, UI
  widgets and consent / legal boilerplate lines (`txt.ocr_page_tokens`);
- >= XM_MIN_AGREE agreeing MinHash positions: with a large page and a short OCR text a single chance agreement
  would otherwise read as a high containment;
- template body: the page text is near-identical (Jaccard >= 0.9) in more than XM_TEMPLATE_MAX_DF incidents (known
  campaigns during assignment) -> no evidence, as for the page-vs-page rule (CR-ASTRA-PIPE-203).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from hydra.contracts import GeneMatch, Provenance

if TYPE_CHECKING:
    from hydra.entropy import Rarity
    from hydra.synapse import IncidentGenome

OCR_CHANNEL = "ocr5"
PAGE_CHANNEL = "page"
XM_MIN_CONTAINMENT = 0.5    # share of the OCR 5-shingles found in the saved page text (measured, E-XM-1)
XM_MIN_OCR_SHINGLES = 30    # word 5-shingles ≈ 35 words of screenshot text after boilerplate removal
XM_MIN_AGREE = 4            # agreeing positions of 128 (J >= 0.031)
XM_TEMPLATE_JACCARD = 0.9   # same cut as synapse.TEXT_NEARDUP_JACCARD
XM_TEMPLATE_MAX_DF = 2      # same cut as synapse.TEXT_TEMPLATE_MAX_DF
XM_BITS = 12.0              # = synapse.NEARDUP_BITS: a near-duplicate page may link on its own


def containment(j: float, n_inner: int, n_outer: int) -> float:
    """|A ∩ B| / |A| for A = `n_inner` shingles, B = `n_outer` shingles, from the MinHash Jaccard estimate `j`."""
    if j <= 0 or n_inner <= 0 or n_outer <= 0:
        return 0.0
    return float(min(1.0, j * (n_inner + n_outer) / (1.0 + j) / n_inner))


def _one_way(o: IncidentGenome, p: IncidentGenome, rarity: Rarity | None):
    """(containment, agree, perm, n_ocr, n_page, df, gene_ocr, gene_page) of o's OCR text in p's page, or None."""
    if OCR_CHANNEL not in o.minhash or PAGE_CHANNEL not in p.minhash or PAGE_CHANNEL in o.minhash:
        return None
    (so, no, go), (sp, np_, gp) = o.minhash[OCR_CHANNEL], p.minhash[PAGE_CHANNEL]
    if len(so) != len(sp) or no < XM_MIN_OCR_SHINGLES:
        return None
    agree = int(np.count_nonzero(so == sp))
    if agree < XM_MIN_AGREE:
        return None
    c = containment(agree / len(so), no, np_)
    if c < XM_MIN_CONTAINMENT:
        return None
    df = rarity.minhash_near_df(gp.value or "", XM_TEMPLATE_JACCARD) if rarity is not None else 1
    if df > XM_TEMPLATE_MAX_DF:
        return None
    return c, agree, len(so), no, np_, df, go, gp


def cross_modal_text(a: IncidentGenome, b: IncidentGenome, rarity: Rarity | None) -> GeneMatch | None:
    """Screenshot OCR text of one incident contained in the saved landing-page text of the other: a cross-modal
    near-duplicate page (XM_BITS, kind `cross_modal_text`), or None. Value / evidence fields follow (a, b)."""
    best = None
    for o, p, o_is_a in ((a, b, True), (b, a, False)):
        r = _one_way(o, p, rarity)
        if r is not None and (best is None or r[0] > best[0][0]):
            best = (r, o_is_a)
    if best is None:
        return None
    (c, agree, perm, no, np_, df, go, gp), o_is_a = best
    vo, vp = f"{OCR_CHANNEL}: {no} shingli", f"{PAGE_CHANNEL}: {np_} shingli"
    expl = (f"Zrzut ekranu tej samej strony: udział shingli tekstu OCR obecnych w zapisanym tekście strony drugiego"
            f" incydentu wynosi {c:.2f} ({no} shingli OCR, {np_} shingli strony; szacunek MinHash, {agree} z {perm}"
            f" zgodnych pozycji); tekst strony niemal identyczny w {df} incydentach korpusu, nie szablon"
            f" ({XM_BITS:+.1f} bit)")
    return GeneMatch(family="TXT", kind="cross_modal_text", value_a=vo if o_is_a else vp,
                     value_b=vp if o_is_a else vo, similarity=round(c, 4), doc_freq=df, bits=XM_BITS,
                     provenance=Provenance.INF, evidence_a=go.evidence if o_is_a else gp.evidence,
                     evidence_b=gp.evidence if o_is_a else go.evidence, explanation_pl=expl)
