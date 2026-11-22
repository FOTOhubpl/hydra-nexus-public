"""ADV-RETR: extra candidate sources for `retrieval._collect` (switch HYDRA_ADV_RETR=1, default off) and the
k-NN overflow telemetry of `retrieval.query_scope` (CR-UI-FINAL-5, always on, proposes nothing).

Both sources only propose pairs; `synapse.score_pair` decides with unchanged rules. Below the all-pairs cut-off
(`retrieval._collect`, n <= all_pairs_below) every pair is already a candidate and the sources add nothing, so the
switch cannot change a small-corpus run (the NASK sample, `sample+syn`, hidden_sim assignment against the sample).

- `lsh:page`, `lsh:ocr_page`: MinHash LSH over the `minhash_sig` signatures (word 5-shingles, 128 permutations,
  `genome.txt.minhash`). Banding is 128 bands of one row: a band key is (position, value), and the number of
  shared band keys of two signatures is computed for all pairs at once as a sparse product M·Mᵀ. A band key held
  by more than `max_df` signatures (the exact index's cap) is not a key: such text is a template body that the
  scorer reads as 0 bits without context evidence anyway. Every pair sharing a key is then verified on the full
  signatures with the scorer's own preconditions and a margin below its cut:
  - page ↔ page: both >= `synapse.TEXT_NEARDUP_MIN_SHINGLES` shingles and Jaccard estimate >= LSH_PAGE_JACCARD
    (scorer: `TEXT_NEARDUP_JACCARD` = 0.9, `_text_near_duplicate`);
  - screenshot OCR ↔ page (`crossmodal._one_way`): OCR incident without a page channel, >= XM_MIN_OCR_SHINGLES
    OCR shingles, >= XM_MIN_AGREE agreeing positions and containment estimate >= LSH_XM_CONTAINMENT
    (scorer: 0.5).
  The 32x4 `minhash_band` genes already reach the exact index, but they are dropped above `max_df` and four-row
  bands need J ≈ 0.5+ per band; the OCR-in-page containment of a short screenshot text has J of a few percent
  and is not reachable by them at all.
- `tfidf:text`: char 3-5-gram TF-IDF (scikit-learn `TfidfVectorizer`, `char_wb`, sublinear tf, IDF fitted on
  the retrieval universe) over the incident's own text gene values (TFIDF_KINDS), cosine top-`knn_k` other
  incidents per querying incident at >= TFIDF_MIN_COS (directional, as `knn:`). The `charsig_*` vectors of
  `txt_phrases` are hashed TF without IDF, per channel; this index weights n-grams by corpus rarity and pools
  the channels of an incident.

Both predicates are pairwise (LSH) or computed per querying row (TF-IDF) on the same universe, so
`candidates_per_query` stays equal to `candidates(..., queries=[q])` (tests/core/test_retrieval_adv.py).
"""

from __future__ import annotations

import os
from collections import defaultdict

import numpy as np

ENV_FLAG = "HYDRA_ADV_RETR"

LSH_CHANNELS = ("page", "ocr5")
LSH_PAGE_JACCARD = 0.8      # scorer cut 0.9 (synapse.TEXT_NEARDUP_JACCARD) minus margin for the estimate
LSH_XM_CONTAINMENT = 0.4    # scorer cut 0.5 (crossmodal.XM_MIN_CONTAINMENT) minus margin
LSH_CHUNK = 2048            # query rows per sparse product
TFIDF_KINDS = frozenset({"phrase", "ocr_line", "page_title", "headline", "lead", "ad_headline", "product_name"})
TFIDF_MIN_COS = 0.5
TFIDF_NGRAM = (3, 5)
TFIDF_MAX_DF = 0.5          # n-grams in more than half of the documents carry no linking signal
TFIDF_CHUNK = 512


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "") == "1"


def collect(genomes: dict, add, *, max_df: int, knn_k: int, qset: set[str] | None, exhaustive: bool) -> None:
    """Called by `retrieval._collect` when `enabled()`. `add(a, b, src, directional)` as there."""
    if exhaustive:
        return
    _lsh(genomes, add, max_df=max_df, qset=qset)
    _tfidf(genomes, add, k=knn_k, qset=qset)


# --------------------------------------------------------------------------------------- MinHash LSH


def _lsh_rows(genomes: dict) -> tuple[list[str], list[str], np.ndarray, np.ndarray, set[str]] | None:
    """(owner, channel, signatures [rows, perm] uint32, shingles, incidents with a page channel)."""
    own: list[str] = []
    chans: list[str] = []
    sigs: list[np.ndarray] = []
    nsh: list[int] = []
    with_page: set[str] = set()
    for iid in sorted(genomes):
        mh = genomes[iid].minhash
        if "page" in mh:
            with_page.add(iid)
        for ch in LSH_CHANNELS:
            if ch in mh:
                sig, n, _g = mh[ch]
                own.append(iid)
                chans.append(ch)
                sigs.append(sig)
                nsh.append(n)
    if len(sigs) < 2:
        return None
    perm = max({len(s) for s in sigs}, key=lambda p: (sum(len(s) == p for s in sigs), p))
    keep = [i for i, s in enumerate(sigs) if len(s) == perm]  # one permutation count (128): positions comparable
    return ([own[i] for i in keep], [chans[i] for i in keep], np.stack([sigs[i] for i in keep]).astype(np.uint32),
            np.array([nsh[i] for i in keep]), with_page)


def _lsh(genomes: dict, add, *, max_df: int, qset: set[str] | None) -> None:
    from scipy import sparse

    from hydra.crossmodal import XM_MIN_AGREE, XM_MIN_OCR_SHINGLES, containment
    from hydra.synapse import TEXT_NEARDUP_MIN_SHINGLES

    got = _lsh_rows(genomes)
    if got is None:
        return
    own, chans, sig, nsh, with_page = got
    rows, perm = sig.shape
    keys = np.arange(perm, dtype=np.uint64)[None, :] * np.uint64(2**32) + sig.astype(np.uint64)
    uniq, col = np.unique(keys.ravel(), return_inverse=True)
    df = np.bincount(col, minlength=len(uniq))
    ok = df[col] <= max_df  # band keys held by more than max_df signatures are not keys (template bodies)
    m = sparse.csr_matrix((np.ones(int(ok.sum()), dtype=np.int32),
                           (np.repeat(np.arange(rows), perm)[ok], col[ok])), shape=(rows, len(uniq)))
    mt = m.T.tocsr()
    own_a = np.array(own)
    is_page = np.array([c == "page" for c in chans])
    ocr_ok = np.array([c == "ocr5" and o not in with_page for c, o in zip(chans, own, strict=True)])
    q_rows = np.arange(rows) if qset is None else np.flatnonzero(np.isin(own_a, list(qset)))
    for s in range(0, len(q_rows), LSH_CHUNK):
        r = q_rows[s:s + LSH_CHUNK]
        c = (m[r] @ mt).tocoo()
        i, j = r[c.row], c.col
        keep = own_a[i] != own_a[j]
        i, j = i[keep], j[keep]
        if not len(i):
            continue
        agree = np.count_nonzero(sig[i] == sig[j], axis=1)  # verification on the full signatures
        for a, b, g in zip(i.tolist(), j.tolist(), agree.tolist(), strict=True):
            if is_page[a] and is_page[b]:
                if min(nsh[a], nsh[b]) >= TEXT_NEARDUP_MIN_SHINGLES and g / perm >= LSH_PAGE_JACCARD:
                    add(own[a], own[b], "lsh:page")
                continue
            o, p = (a, b) if ocr_ok[a] and is_page[b] else (b, a) if ocr_ok[b] and is_page[a] else (None, None)
            if o is None:
                continue
            if (nsh[o] >= XM_MIN_OCR_SHINGLES and g >= XM_MIN_AGREE
                    and containment(g / perm, int(nsh[o]), int(nsh[p])) >= LSH_XM_CONTAINMENT):
                add(own[a], own[b], "lsh:ocr_page")


# --------------------------------------------------------------------------------------- char n-gram TF-IDF


def _docs(genomes: dict) -> tuple[list[str], list[str]]:
    ids, docs = [], []
    for iid in sorted(genomes):
        vals = sorted({v for (k, v) in genomes[iid].discrete if k in TFIDF_KINDS and v})
        if vals:
            ids.append(iid)
            docs.append("\n".join(vals))
    return ids, docs


def _tfidf(genomes: dict, add, *, k: int, qset: set[str] | None) -> None:
    from sklearn.feature_extraction.text import TfidfVectorizer

    ids, docs = _docs(genomes)
    if len(docs) < 2:
        return
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=TFIDF_NGRAM, min_df=2,
                          max_df=max(2, int(TFIDF_MAX_DF * len(docs))), sublinear_tf=True, dtype=np.float32)
    try:
        x = vec.fit_transform(docs).tocsr()
    except ValueError:  # empty vocabulary after pruning
        return
    xt = x.T.tocsr()
    ids_a = np.array(ids)
    q_rows = np.arange(len(ids)) if qset is None else np.flatnonzero(np.isin(ids_a, list(qset)))
    for s in range(0, len(q_rows), TFIDF_CHUNK):
        r = q_rows[s:s + TFIDF_CHUNK]
        sims = (x[r] @ xt).toarray()
        sims[np.arange(len(r)), r] = -1.0
        for row, qi in zip(sims, r.tolist(), strict=True):
            for j in np.argsort(-row, kind="stable")[:k]:
                if row[j] >= TFIDF_MIN_COS:
                    add(ids[qi], ids[j], "tfidf:text", True)


# --------------------------------------------------------------------------------------- telemetry


def knn_overflow(genomes: dict, rarity, query: str, k: int) -> dict:
    """CR-UI-FINAL-5: per embedding kind, other incidents whose best vector similarity to `query` reaches the
    k-NN similarity floor of `retrieval._collect` (`cut`), and how many of them fell outside the top-k. Counts
    only; proposes no pair. Same per-incident best similarity as `retrieval.knn_incident_pairs`."""
    g = genomes.get(query)
    above: dict[str, int] = defaultdict(int)
    beyond: dict[str, int] = defaultdict(int)
    if g is None:
        return {"knn_above_floor": {}, "knn_beyond_k": {}, "knn_beyond_k_total": 0}
    for key, (qmat, _genes) in sorted(g.vectors.items()):
        mats, owners = [], []
        for iid, other in genomes.items():
            if iid != query and key in other.vectors:
                mats.append(other.vectors[key][0])
                owners.append(iid)
        if not mats:
            continue
        kind, model = key
        bg = rarity.backgrounds.get((kind, model))
        cut = bg.quantile(0.99) if bg is not None and len(bg.sims) >= 100 else 0.9
        q = np.asarray(qmat, dtype=np.float32)
        q = q.reshape(-1, q.shape[-1])
        x = np.concatenate([np.asarray(mt, dtype=np.float32).reshape(-1, q.shape[1]) for mt in mats])
        starts = np.r_[0, np.cumsum([np.asarray(mt).reshape(-1, q.shape[1]).shape[0] for mt in mats])[:-1]]
        best = np.maximum.reduceat((q @ x.T).max(axis=0), starts)
        n_above = int(np.count_nonzero(best >= cut))
        above[kind] += n_above
        beyond[kind] += max(0, n_above - k)
    return {"knn_above_floor": dict(sorted(above.items())), "knn_beyond_k": dict(sorted(beyond.items())),
            "knn_beyond_k_total": int(sum(beyond.values()))}
