"""Cross-modal page text (CR-OPGENES-12): screenshot OCR text vs saved landing-page text. Synthetic text only."""

from __future__ import annotations

import random

import numpy as np

from hydra.contracts import EvidencePointer, Family
from hydra.crossmodal import XM_BITS, cross_modal_text
from hydra.genome.txt import minhash, ocr_page_tokens, shingles
from hydra.genome.txt_norm import fold
from hydra.genome.txt_sources import IncidentTexts, Segment, TextBlock
from tests.adversarial.helpers import corpus, gene

_SYLL = ["ka", "ro", "mi", "te", "lu", "pa", "no", "si", "we", "do", "za", "be", "gor", "tan", "wil", "mesz"]


def _words(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    return ["".join(rng.choice(_SYLL) for _ in range(rng.randint(2, 4))) for _ in range(n)]


def _sig_gene(iid: str, ch: str, words: list[str]):
    shs = shingles(fold(" ".join(words)).split(), 5)
    g = gene(iid, "minhash_sig", f"{ch}:" + np.asarray(minhash(shs).hashvalues, dtype=">u4").tobytes().hex(),
             f"TXT:{'label' if ch == 'ocr5' else 'phrase'}:minhash_{ch}", Family.TXT)
    g.evidence = EvidencePointer(artifact_id=f"{iid}-{ch}", note=f"channel={ch}; shingles={len(shs)}; synthetic")
    return g


def _ocr_of(words: list[str], start: int, n: int, seed: int, errors: float = 0.03) -> list[str]:
    """A screenshot shows a window of the page; OCR garbles a few tokens."""
    rng = random.Random(seed)
    return [w + "x" if rng.random() < errors else w for w in words[start:start + n]]


def _rows(pages: dict[str, list[str]], shots: dict[str, list[str]]):
    rows = {iid: [_sig_gene(iid, "page", w)] for iid, w in pages.items()}
    for iid, w in shots.items():
        rows[iid] = [_sig_gene(iid, "ocr5", w)]
    return rows


def test_screenshot_of_the_same_page_links():
    page = _words(1, 900)
    genomes, rarity = corpus(_rows({"known": page, "other": _words(2, 900)}, {"shot": _ocr_of(page, 50, 250, 3)}))
    m = cross_modal_text(genomes["shot"], genomes["known"], rarity)
    assert m is not None and m.kind == "cross_modal_text" and m.bits == XM_BITS and str(m.family) == "TXT"
    assert m.similarity >= 0.5 and m.explanation_pl.endswith(f"({XM_BITS:+.1f} bit)") and "%" not in m.explanation_pl
    assert m.value_a.startswith("ocr5:") and m.evidence_a.artifact_id == "shot-ocr5"
    m2 = cross_modal_text(genomes["known"], genomes["shot"], rarity)  # either argument order; fields follow (a, b)
    assert m2 is not None and m2.value_b.startswith("ocr5:") and m2.evidence_b.artifact_id == "shot-ocr5"
    assert cross_modal_text(genomes["shot"], genomes["other"], rarity) is None


def test_unrelated_text_does_not_link():
    genomes, rarity = corpus(_rows({"known": _words(10, 900)}, {"shot": _words(11, 250)}))
    assert cross_modal_text(genomes["shot"], genomes["known"], rarity) is None


def test_body_shared_by_more_than_two_incidents_does_not_link():
    """A template body near-identical in three incidents is not evidence of one operator (CR-ASTRA-PIPE-203)."""
    page = _words(20, 900)
    pages = {f"tpl-{i}": page for i in range(3)}
    genomes, rarity = corpus(_rows(pages, {"shot": _ocr_of(page, 0, 250, 21)}))
    assert cross_modal_text(genomes["shot"], genomes["tpl-0"], rarity) is None
    genomes, rarity = corpus(_rows({"tpl-0": page, "tpl-1": page}, {"shot": _ocr_of(page, 0, 250, 21)}))
    assert cross_modal_text(genomes["shot"], genomes["tpl-0"], rarity) is not None  # two copies: still rare


def test_short_ocr_does_not_link():
    page = _words(30, 120)
    genomes, rarity = corpus(_rows({"known": page}, {"shot": page[10:30]}))  # 16 shingles < 30
    assert cross_modal_text(genomes["shot"], genomes["known"], rarity) is None


def test_two_saved_pages_are_left_to_the_page_rule():
    """The OCR side must not have a saved page of its own: page vs page is the Jaccard >= 0.9 rule."""
    page = _words(40, 900)
    rows = _rows({"known": page}, {"both": _ocr_of(page, 0, 250, 41)})
    rows["both"].append(_sig_gene("both", "page", _words(42, 300)))
    genomes, rarity = corpus(rows)
    assert cross_modal_text(genomes["both"], genomes["known"], rarity) is None


def test_ocr_tokens_skip_chrome_noise_zones_and_consent_boilerplate():
    ev = EvidencePointer(artifact_id="a1")

    def seg(t, **kw):
        return Segment(t, t, ev, **kw)

    texts = IncidentTexts(blocks=[
        TextBlock("ad", "ocr_image", "a1", [
            seg("Nowy sposób oszczędzania pieniędzy"), seg("Sponsorowane", chrome=True),
            seg("Używamy plików cookie, aby poprawić działanie serwisu"), seg("Akceptuję wszystkie"),
            seg("Polecane w sieci", zone="feed"), seg("Zarejestruj się dzisiaj")]),
        TextBlock("asset", "ocr_image", "a2", [seg("tekst obrazu na stronie")]),
        TextBlock("ad", "meta_description", "a3", [seg("opis z metadanych")]),
        TextBlock("page", "html_body", "a4", [seg("treść zapisanej strony")]),
    ])
    assert ocr_page_tokens(texts) == ["nowy", "sposob", "oszczedzania", "pieniedzy", "zarejestruj", "sie", "dzisiaj"]
