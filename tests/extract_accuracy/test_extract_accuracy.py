"""F01 harness checks (SYNTHETIC): generator validity, scoring arithmetic and regression floors of the product path.

Floors sit well below the values measured by scripts/measure_extraction.py (docs/release/EXTRACTION_ACCURACY.md):
they catch a broken extractor or harness, not a one-point drift. Known defects are listed there as CR-F01-n and
are not asserted here either way.
"""

from __future__ import annotations

import math
import random

import pytest

from hydra.genome.ocr_pool import OcrPool
from hydra.genome.operator_ids import b58check_payload, bech32_check, check_wallet, iban_mod97
from hydra.ml.ocr import OcrLine

from . import harness as H


def test_wilson_interval():
    lo, hi = H.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = H.wilson(10, 10)
    assert lo == pytest.approx(0.7225, abs=1e-3) and hi == pytest.approx(1.0)
    lo, hi = H.wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    assert all(math.isnan(x) for x in H.wilson(0, 0))


def test_counts_arithmetic():
    c = H.Counts(tp=8, fp=2, fn=2, fp_hq=1)
    assert c.precision == 0.8 and c.recall == 0.8 and c.f1 == pytest.approx(0.8)
    assert c.precision_hq == pytest.approx(8 / 9)
    assert math.isnan(H.Counts().precision) and math.isnan(H.Counts().f1)


def test_generated_identifiers_are_valid():
    """The generator's 'valid' accounts and wallets pass independent and product checks; decoys fail them."""
    g = H.Gen(7)
    rng = random.Random(7)
    for _ in range(20):
        ib = g.pl_iban()
        assert len(ib) == 28 and iban_mod97(ib)
        assert check_wallet("btc", H.b58check(b"\x00" + rng.randbytes(20))).valid
        assert b58check_payload(H.b58check(b"\x41" + rng.randbytes(20))) is not None
        assert bech32_check(H.bech32_segwit_v0("bc", rng.randbytes(20))) is not None
    for v in H.EIP55_VECTORS:
        assert check_wallet("eth", v).valid and "EIP-55" in check_wallet("eth", v).reason
    for ib in H.FOREIGN_IBANS:
        assert iban_mod97(ib)


def test_corpus_has_truth_and_decoys(tmp_path):
    cases, docs = H.make_corpus(tmp_path, seed=3, n_docs=6, n_ocr_docs=1, n_har=2, n_url=3, ocr_sizes=(20,),
                                ocr_formats=("png", "jpg50"))
    assert len(cases) == 6 * len(H.TEXT_CARRIERS) + 2 + 2 + 3
    assert all(d.facts and d.decoys and len(d.page_facts) >= 4 for d in docs)
    kinds = {k for d in docs for k, _ in d.facts}
    assert {"domain", "phone", "price"} <= kinds
    assert all(c.folder.is_dir() and any(c.folder.iterdir()) for c in cases)
    ios = next(c for c in cases if c.carrier == "chat_wa_ios")
    assert (ios.folder / "czat.txt").read_text("utf-8").startswith("[")  # the '[' chat must not be read as JSON


def _run_all(cases, settings, pool=None):
    results = [H.run_case(c, settings, ocr_pool=pool) for c in cases]
    scores = [s for r in results for s in H.score_case(r)]
    return results, scores


def test_product_path_floors(tmp_path, settings):
    cases, _ = H.make_corpus(tmp_path / "corpus", seed=11, n_docs=8, n_ocr_docs=0, n_har=4, n_url=6)
    results, scores = _run_all(cases, settings)
    assert not [r["failed"] for r in results if r["failed"]]
    assert not [r["import_errors"] for r in results if r["import_errors"]]
    roles = {r["case"].carrier: r["roles"] for r in results}
    assert roles["html"] == ["landing_html"] and roles["har_3xx"] == ["har"] and roles["url"] == ["url"]
    assert all(roles[c] == ["text"] for c in H.TEXT_CARRIERS if c.startswith("chat_") or c == "txt")
    by_ex = H.aggregate(scores, ("extractor",))
    by_kind = H.aggregate(scores, ("extractor", "kind"))
    assert by_ex[("txt_entities",)].recall >= 0.8 and by_ex[("txt_entities",)].precision_hq >= 0.8
    assert by_ex[("txt_lines",)].recall >= 0.8
    assert by_ex[("inf_dom",)].recall >= 0.8
    assert by_kind[("inf_dom", "tracker")].recall >= 0.9 and by_kind[("inf_dom", "google_ads")].recall >= 0.9
    assert by_kind[("inf_har", "tracker")].recall >= 0.9
    assert by_kind[("inf_url", "ad_campaign_id")].recall >= 0.9 and by_kind[("inf_url", "ad_campaign_id")].fp == 0
    for kind in ("phone", "email", "domain", "iban", "wallet", "telegram", "price"):
        c = by_kind[("txt_entities", kind)]
        assert c.recall >= 0.7, (kind, c)
    chat = H.aggregate([s for s in scores if s.extractor == "txt_chat"], ("carrier", "kind"))
    for car in ("chat_wa_ios", "chat_wa_android", "chat_tg_json", "chat_json_list"):
        assert chat[(car, "chat_platform")].recall == 1.0, car
        assert chat[(car, "chat_platform")].fp == 0, car


def test_ocr_path_with_echo_engine(tmp_path, settings):
    """Screenshot -> OCR -> txt_entities with an engine that returns the rendered lines verbatim: checks the OCR
    carrier, the incident planning of images and the scoring, independently of the OCR model (absent in tests)."""
    cases, docs = H.make_corpus(tmp_path / "corpus", seed=5, n_docs=4, n_ocr_docs=4, n_har=0, n_url=0,
                                ocr_sizes=(20,), ocr_formats=("png",), carriers=())
    assert len(cases) == 4
    scores = []
    for c, d in zip(cases, docs, strict=True):
        lines = list(d.lines)

        def echo(im, lines=lines):
            return [OcrLine(t, (10, 10 + 40 * i, 900, 40 + 40 * i), 0.95, "echo") for i, t in enumerate(lines)]

        r = H.run_case(c, settings, ocr_pool=OcrPool(None, engine_fn=echo))
        assert not r["failed"] and r["roles"] in (["ad_creative"], ["feed_screenshot"])
        scores += H.score_case(r)
    tot = H.aggregate([s for s in scores if s.extractor == "txt_entities"], ("extractor",))[("txt_entities",)]
    assert tot.recall >= 0.8 and tot.precision_hq >= 0.8
