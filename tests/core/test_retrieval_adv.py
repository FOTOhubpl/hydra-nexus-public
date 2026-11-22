"""ADV-RETR: MinHash LSH and char n-gram TF-IDF candidate sources (HYDRA_ADV_RETR=1) and the k-NN overflow
telemetry of `query_scope` (CR-UI-FINAL-5). Synthetic text and vectors only."""

from __future__ import annotations

import numpy as np
import pytest

from hydra import retrieval_adv
from hydra.contracts import Family
from hydra.crossmodal import cross_modal_text
from hydra.pipeline import rarity_for
from hydra.retrieval import candidates, candidates_per_query, query_scope
from hydra.synapse import build_genome
from tests.adversarial.helpers import gene
from tests.core.test_crossmodal import _ocr_of, _sig_gene, _words

N_FILL = 70  # above the all-pairs cut-off (60)


def _fill(rows: dict) -> dict:
    for k in range(N_FILL):
        iid = f"fill-{k:03}"
        rows[iid] = [gene(iid, "host", f"{iid}.invalid", "INF:operator:host"),
                     _sig_gene(iid, "page", _words(1000 + k, 300)),
                     gene(iid, "headline", " ".join(_words(5000 + k, 8)), "TXT:phrase:headline", Family.TXT)]
    return rows


def _genomes(rows, vectors=None):
    g = {iid: build_genome(iid, gs, vectors or {}) for iid, gs in rows.items()}
    return g, rarity_for(g)


@pytest.fixture
def adv(monkeypatch):
    monkeypatch.setenv(retrieval_adv.ENV_FLAG, "1")


def _text_corpus():
    page = _words(1, 900)
    near = list(page)
    for i in range(0, 900, 60):  # a few edits: Jaccard of 5-shingles stays above 0.8
        near[i] = near[i] + "q"
    rows = {"page-a": [_sig_gene("page-a", "page", page)], "page-b": [_sig_gene("page-b", "page", near)],
            "shot": [_sig_gene("shot", "ocr5", _ocr_of(page, 50, 250, 3))]}
    txt = "Super okazja inwestycyjna tylko dziś zarejestruj się teraz"
    rows["ad-1"] = [gene("ad-1", "headline", txt, "TXT:phrase:headline", Family.TXT)]
    rows["ad-2"] = [gene("ad-2", "headline", txt.replace("dziś", "dzis"), "TXT:phrase:headline", Family.TXT)]
    return _genomes(_fill(rows))


def test_flag_off_adds_nothing(monkeypatch):
    monkeypatch.delenv(retrieval_adv.ENV_FLAG, raising=False)
    g, r = _text_corpus()
    cand = candidates(g, r)
    assert not any(s.startswith(("lsh:", "tfidf:")) for src in cand.values() for s in src)
    assert ("page-a", "shot") not in cand  # today's indexes cannot reach the OCR-in-page containment


def test_lsh_and_tfidf_propose_the_scorer_relevant_pairs(adv):
    g, r = _text_corpus()
    cand = candidates(g, r)
    assert "lsh:page" in cand[("page-a", "page-b")]
    assert "lsh:ocr_page" in cand[("page-a", "shot")]
    assert cross_modal_text(g["shot"], g["page-a"], r) is not None  # the scorer links what LSH proposed
    assert "tfidf:text" in cand[("ad-1", "ad-2")]
    fills = [k for k, src in cand.items() if k[0].startswith("fill") and k[1].startswith("fill")]
    assert not any(s.startswith(("lsh:", "tfidf:")) for k in fills for s in cand[k])  # unrelated texts


def test_query_mode_equals_per_query(adv):
    g, r = _text_corpus()
    per = candidates_per_query(g, r)
    for q in ("page-a", "page-b", "shot", "ad-1", "ad-2", "fill-000"):
        assert per[q] == candidates(g, r, queries=[q])


def test_exhaustive_corpus_unchanged(adv, monkeypatch):
    rows = {"page-a": [_sig_gene("page-a", "page", _words(1, 900))],
            "page-b": [_sig_gene("page-b", "page", _words(1, 900))]}
    g, r = _genomes(rows)
    on = candidates(g, r)
    monkeypatch.delenv(retrieval_adv.ENV_FLAG)
    assert on == candidates(g, r)


def test_lsh_key_cap_skips_template_bodies(adv):
    body = _words(7, 600)
    rows = {f"tpl-{k:03}": [_sig_gene(f"tpl-{k:03}", "page", body)] for k in range(40)}
    g, r = _genomes(_fill(rows))
    got = {k for k, src in candidates(g, r, max_df=20).items() if "lsh:page" in src}
    assert not got  # every band key held by 40 > max_df signatures


def test_query_scope_counts_knn_overflow():
    dim = 8
    base = np.zeros(dim, dtype=np.float32)
    base[0] = 1.0
    vectors, rows = {}, {}
    for k in range(80):
        iid = f"v{k:03}"
        v = base.copy() if k < 15 else np.eye(dim, dtype=np.float32)[1 + k % (dim - 1)]
        vectors[f"ref-{iid}"] = ("model-x", v)
        rows[iid] = [gene(iid, "dinov2", None, "VIS:asset:emb", Family.VIS, vector_ref=f"ref-{iid}")]
    g, r = _genomes(rows, vectors)
    s = query_scope(g, r, "v000", knn_k=10)
    assert s["knn_above_floor"] == {"dinov2": 14} and s["knn_beyond_k"] == {"dinov2": 4}
    assert s["knn_beyond_k_total"] == 4
    assert "knn_top_k" in s["bounded_by"]
    small = {i: g[i] for i in sorted(g)[:20]}
    assert "knn_beyond_k" not in query_scope(small, rarity_for(small), "v000")  # all-pairs mode: unchanged keys
