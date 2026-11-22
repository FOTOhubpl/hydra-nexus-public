"""M3 RETRIEVAL: candidate incident pairs from the union of indexes (PLAN_MAX §1.6: recall is the lever).

Indexes:
- `exact:<kind>`  inverted index on discrete gene values (stop-genes and values held by more than
                  `max_df` incidents skipped: they cannot be rare evidence). `max_df` = 20 % of the corpus, at most
                  `MAX_DF_ABS`: one value held by d incidents emits d(d-1)/2 pairs, so without the absolute cap the
                  index alone is quadratic in the corpus size (10k incidents: up to 2M pairs per value);
- `near:<kind>`   Hamming search on hex hashes (pdq, phash…), vectorised numpy;
- `knn:<kind>`    exact cosine top-k per (kind, model) with `hydra.ml.models.cosine_topk` (no FAISS).
Each candidate pair carries the set of indexes that proposed it (`PairScore.candidate_sources`).
When the corpus is small (≤ `all_pairs_below` incidents) every pair is a candidate (`all_pairs`).

Backstop (`backstop:<kind>`, CR-ASTRA-PIPE-201): a value above `max_df` is dropped from the exact index, but an
operator-tier value (INF/META `<family>:operator:<name>`, or an identical original file) held by a large campaign is
exactly the evidence that links it; dropping every posting split a supported 13-member campaign into singletons
once the corpus grew from 60 to 61 incidents. Such postings still emit a bounded candidate set: every pair while
the value has at most `BACKSTOP_FULL_DF` holders; above that, for the whole corpus a sparse connected ring (each
holder to its next `BACKSTOP_RING_K` holders, grouped by campaign unit, hashed order inside a unit), and in query
mode (assignment, leave-one-out) the query to at most `BACKSTOP_QUERY_CAP` holders picked round-robin over campaign
units (`Rarity.kit_unit_of`), so every campaign holding the value stays reachable. Cost per value: at most
`BACKSTOP_FULL_DF`·(`BACKSTOP_FULL_DF`−1)/2 pairs, or df·`BACKSTOP_RING_K` (corpus) / `BACKSTOP_QUERY_CAP` per
query. Scoring is unchanged: the backstop only proposes pairs, `score_pair` decides with the same rarity bits.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections import defaultdict
from itertools import combinations

import numpy as np

from hydra.entropy import Rarity, is_stop
from hydra.synapse import IncidentGenome, gene_tier, hamming_matrix, near_hash_spec

Pair = tuple[str, str]

MAX_DF_ABS = 200  # >= 0.2 x 837 (largest corpus measured): corpora up to 1000 incidents are unchanged
BACKSTOP_FULL_DF = 64     # operator-tier value above max_df: all pairs up to 64 holders (<= 2016 pairs per value)
BACKSTOP_RING_K = 4       # above that: each holder to its next 4 holders (connected, df x 4 pairs per value)
BACKSTOP_QUERY_CAP = 64   # query mode: at most 64 other holders per value, round-robin over campaign units
BACKSTOP_FAMILIES = {"INF", "META"}  # operator tier links on its own only in these families (synapse.SUM_FAMILIES)


def _key(a: str, b: str) -> Pair:
    return (a, b) if a < b else (b, a)


def candidates(genomes: dict[str, IncidentGenome], rarity: Rarity, *, max_df: int | None = None, knn_k: int = 10,
               all_pairs_below: int = 60, queries: list[str] | None = None) -> dict[Pair, set[str]]:
    """Candidate pairs. `queries`: restrict to pairs touching these incidents (open-set assignment)."""
    qset = set(queries) if queries is not None else None
    out: dict[Pair, set[str]] = defaultdict(set)

    def add(a: str, b: str, src: str, _directional: bool = False) -> None:
        if a == b:
            return
        if qset is not None and a not in qset and b not in qset:
            return
        out[_key(a, b)].add(src)

    if qset is None:  # whole corpus: pairs of a sorted holder list go in directly (same keys, same order)
        def sorted_pairs(pairs, src: str) -> None:
            for k in pairs:
                out[k].add(src)

        add.sorted_pairs = sorted_pairs  # type: ignore[attr-defined]
    _collect(genomes, rarity, add, max_df=max_df, knn_k=knn_k, all_pairs_below=all_pairs_below, qset=qset)
    return dict(out)


def candidates_per_query(genomes: dict[str, IncidentGenome], rarity: Rarity, *, max_df: int | None = None,
                         knn_k: int = 10, all_pairs_below: int = 60) -> dict[str, dict[Pair, set[str]]]:
    """For every incident q: exactly `candidates(genomes, rarity, queries=[q])`, computed in one pass (leave-one-out
    self-assignment of a whole run). Exact / near / all-pairs candidates are symmetric and go to both incidents;
    a kNN candidate goes to the querying incident only, as in query mode."""
    out: dict[str, dict[Pair, set[str]]] = {i: defaultdict(set) for i in genomes}

    def add(a: str, b: str, src: str, directional: bool = False) -> None:
        if a == b:
            return
        k = _key(a, b)
        out[a][k].add(src)
        if not directional:
            out[b][k].add(src)

    _collect(genomes, rarity, add, max_df=max_df, knn_k=knn_k, all_pairs_below=all_pairs_below, qset=None,
             per_query=True)
    return {i: dict(v) for i, v in out.items()}


def _collect(genomes: dict[str, IncidentGenome], rarity: Rarity, add, *, max_df: int | None, knn_k: int,
             all_pairs_below: int, qset: set[str] | None, per_query: bool = False) -> None:
    ids = sorted(genomes)

    n = len(ids)
    if n <= all_pairs_below:
        _emit_sorted(add, combinations(ids, 2), "all_pairs")
    max_df = max_df or min(MAX_DF_ABS, max(5, int(0.2 * n)))

    units = rarity.kit_unit_of or {}
    for (kind, value), holders in rarity.postings.items():
        if len(holders) < 2 or (qset is not None and qset.isdisjoint(holders)):
            continue
        if is_stop(kind, value):
            continue
        if len(holders) > max_df:
            if n > all_pairs_below and _backstop_eligible(genomes, holders, (kind, value)):
                _backstop_pairs(sorted(h for h in holders if h in genomes), (kind, value), units, qset, add,
                                per_query)
            continue
        hs = sorted(h for h in holders if h in genomes)
        if qset is None:
            _emit_sorted(add, combinations(hs, 2), f"exact:{kind}")
        else:
            _query_pairs(hs, qset, f"exact:{kind}", add)

    # near-duplicate hashes
    by_kind: dict[str, list[tuple[np.ndarray, str]]] = defaultdict(list)
    for iid, g in genomes.items():
        for kind, (arr, _genes) in g.near.items():
            for row in arr:
                by_kind[kind].append((row, iid))
    for kind, rows in by_kind.items():
        spec = near_hash_spec(kind)
        if spec is None or len(rows) < 2:
            continue
        x = np.stack([r[0] for r in rows])
        own = np.array([r[1] for r in rows])
        q_rows = np.arange(len(x)) if qset is None else np.flatnonzero(np.isin(own, list(qset)))
        for s in range(0, len(q_rows), 64):
            r = q_rows[s : s + 64]
            d = hamming_matrix(x[r], x)
            ii, jj = np.nonzero(d <= spec[1])
            ii = r[ii]
            hit = own[ii] != own[jj]  # same filter and order as the per-element loop, one numpy compare
            src = f"near:{kind}"
            for a, b in zip(own[ii[hit]].tolist(), own[jj[hit]].tolist(), strict=True):
                add(a, b, src)

    # embeddings
    vec_keys: dict[tuple[str, str], list[tuple[np.ndarray, str]]] = defaultdict(list)
    for iid, g in genomes.items():
        for key, (mat, _genes) in g.vectors.items():
            for row in mat:
                vec_keys[key].append((row, iid))
    for (kind, model), rows in vec_keys.items():
        if len(rows) < 2:
            continue
        x = np.stack([r[0] for r in rows]).astype(np.float32)
        own = np.array([r[1] for r in rows])
        bg = rarity.backgrounds.get((kind, model))
        cut = bg.quantile(0.99) if bg is not None and len(bg.sims) >= 100 else 0.9
        for a, b in knn_incident_pairs(x, own, k=knn_k, cut=cut, queries=qset):
            add(a, b, f"knn:{kind}", True)

    from hydra import retrieval_adv  # ADV-RETR extra sources (HYDRA_ADV_RETR=1, default off)

    if retrieval_adv.enabled():
        retrieval_adv.collect(genomes, add, max_df=max_df, knn_k=knn_k, qset=qset, exhaustive=n <= all_pairs_below)


def query_scope(genomes: dict[str, IncidentGenome], rarity: Rarity, query: str, *, max_df: int | None = None,
                knn_k: int = 10, all_pairs_below: int = 60) -> dict:
    """Read-only retrieval-scope telemetry for one query (U03, ASTRA STATUS_2026-11-22): which of the query's
    discrete postings the candidate generator of `candidates(..., queries=[query])` skipped or truncated, with the
    same decisions as `_collect`. Counts and gene kinds only, never gene values. Proposes no pair and changes no
    score; `truncated` means the candidate set is a bounded sample, not that a campaign was missed."""
    n = len(genomes)
    mdf = max_df or min(MAX_DF_ABS, max(5, int(0.2 * n)))
    exhaustive = n <= all_pairs_below
    g = genomes.get(query)
    keys = sorted(g.discrete) if g is not None else []
    shared = stop = exact = backstop_full = 0
    capped: dict[str, int] = defaultdict(int)
    dropped: dict[str, int] = defaultdict(int)
    holders_not_reached = 0
    for key in keys:
        holders = rarity.postings.get(key) or set()
        if len(holders) < 2 or query not in holders:
            continue
        shared += 1
        if is_stop(*key):
            stop += 1
            continue
        if len(holders) <= mdf:
            exact += 1
            continue
        if exhaustive:
            continue  # over max_df, but every pair is an `all_pairs` candidate anyway
        hs = [h for h in holders if h in genomes]
        if not _backstop_eligible(genomes, holders, key):
            dropped[key[0]] += 1
            holders_not_reached += len(hs) - 1
        elif len(hs) <= BACKSTOP_FULL_DF:
            backstop_full += 1
        else:
            capped[key[0]] += 1
            holders_not_reached += max(0, len(hs) - 1 - BACKSTOP_QUERY_CAP)
    reasons = []
    if dropped:
        reasons.append("postings_over_max_df_dropped")
    if capped:
        reasons.append("backstop_query_cap")
    if not exhaustive:
        reasons.append("knn_top_k")
    from hydra.retrieval_adv import knn_overflow  # CR-UI-FINAL-5: neighbours above the floor beyond k

    knn = knn_overflow(genomes, rarity, query, knn_k) if not exhaustive else {}
    return {"corpus_incidents": n, "mode": "all_pairs" if exhaustive else "indexed",
            "all_pairs_below": all_pairs_below, "max_df": mdf, "knn_k": knn_k,
            "backstop_full_df": BACKSTOP_FULL_DF, "backstop_query_cap": BACKSTOP_QUERY_CAP,
            "postings_shared": shared, "postings_stop": stop, "postings_exact": exact,
            "postings_backstop_full": backstop_full, "postings_backstop_capped": dict(sorted(capped.items())),
            "postings_dropped": dict(sorted(dropped.items())), "holder_slots_not_reached": holders_not_reached,
            "truncated": bool(dropped or capped), "bounded_by": reasons, **knn}


def _backstop_eligible(genomes: dict[str, IncidentGenome], holders: set[str], key: tuple[str, str]) -> bool:
    """Operator-tier INF/META value or identical original file (synapse.level == campaign on its own)."""
    for h in holders:
        g = genomes.get(h)
        if g is None:
            continue
        for gene in g.discrete.get(key, ()):
            if gene.kind == "artifact_sha256" or (gene_tier(gene) == "operator" and str(gene.family) in
                                                  BACKSTOP_FAMILIES):
                return True
        return False  # one holder decides: the same (kind, value) carries the same group in every incident
    return False


def _unit_lanes(hs: list[str], key: tuple[str, str], units: dict[str, str]) -> list[list[str]]:
    """Holders per campaign unit (no unit: the incident is its own unit), each lane in a deterministic order
    hashed with the value (no lexicographic id bias)."""
    salt = f"{key[0]}\x1f{key[1]}\x1f".encode()
    lanes: dict[str, list[str]] = defaultdict(list)
    for digest, h in sorted((hashlib.blake2b(salt + h.encode(), digest_size=8).hexdigest(), h) for h in hs):
        lanes[units.get(h) or "\x00" + digest].append(h)
    return [lanes[u] for u in sorted(lanes)]


def _round_robin(lanes: list[list[str]], skip: str, cap: int) -> list[str]:
    """Up to `cap` holders other than `skip`: the first of every unit, then the second, ..."""
    out: list[str] = []
    depth = 0
    while len(out) < cap:
        row = [lane[depth] for lane in lanes if depth < len(lane)]
        if not row:
            break
        out += [h for h in row if h != skip]
        depth += 1
    return out[:cap]


def _backstop_pairs(hs: list[str], key: tuple[str, str], units: dict[str, str], qset: set[str] | None,
                    add, per_query: bool) -> None:
    src = f"backstop:{key[0]}"
    if len(hs) <= BACKSTOP_FULL_DF:
        if qset is None:
            _emit_sorted(add, combinations(hs, 2), src)
        else:
            _query_pairs(hs, qset, src, add)
        return
    lanes = _unit_lanes(hs, key, units)
    if qset is None and not per_query:  # whole corpus: a sparse ring keeps every holder connected
        ring = [h for lane in lanes for h in lane]
        n = len(ring)
        for i, a in enumerate(ring):
            for step in range(1, BACKSTOP_RING_K + 1):
                add(a, ring[(i + step) % n], src)
        return
    for q in (hs if qset is None else sorted(qset.intersection(hs))):  # query: campaign-aware bounded sample
        for h in _round_robin(lanes, q, BACKSTOP_QUERY_CAP):
            add(q, h, src, True)


def _emit_sorted(add, pairs, src: str) -> None:
    """`add(a, b, src)` for every pair of `combinations(<sorted unique ids>, 2)` (a < b), or the collector's
    bulk insert of the same keys in the same order when it has one (`candidates` over the whole corpus)."""
    bulk = getattr(add, "sorted_pairs", None)
    if bulk is not None:
        bulk(pairs, src)
        return
    for a, b in pairs:
        add(a, b, src)


def _query_pairs(hs: list[str], qset: set[str], src: str, add) -> None:
    """The pairs of `combinations(hs, 2)` that touch `qset`, in the same order, in O(len(hs) x |qset|)."""
    qpos = [i for i, h in enumerate(hs) if h in qset]
    if not qpos:
        return
    qs = set(qpos)
    for i, a in enumerate(hs):
        if i in qs:
            for b in hs[i + 1:]:
                add(a, b, src)
        else:
            for j in qpos[bisect_right(qpos, i):]:
                add(a, hs[j], src)


def knn_incident_pairs(x: np.ndarray, own: np.ndarray, *, k: int, cut: float, queries: set[str] | None = None,
                       chunk: int = 2048) -> list[tuple[str, str]]:
    """Per query incident: the top-k *other incidents* by best vector similarity (≥ `cut`).

    Same-incident columns are masked before ranking and similarities are aggregated per incident, so an
    incident with hundreds of vectors (video frames) cannot crowd its own neighbours out of the top-k.
    In assignment mode only the rows of `queries` are searched."""
    ids, inv = np.unique(own, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    xs, invs = x[order], inv[order]
    starts = np.r_[0, np.flatnonzero(np.diff(invs)) + 1]
    ends = np.r_[starts[1:], len(xs)]
    q_inc = range(len(ids)) if queries is None else np.flatnonzero(np.isin(ids, list(queries)))
    out: list[tuple[str, str]] = []
    for qi in q_inc:  # one matmul per query incident: memory O(rows_qi x V), never O(n_incidents^2)
        lo, hi = starts[qi], ends[qi]
        best = np.full(len(ids), -2.0, dtype=np.float32)
        for s in range(lo, hi, chunk):
            sims = xs[s : min(hi, s + chunk)] @ xs.T
            sims[:, lo:hi] = -2.0
            np.maximum(best, np.maximum.reduceat(sims, starts, axis=1).max(axis=0), out=best)
        best[qi] = -2.0
        for j in np.argsort(-best)[:k]:
            if best[j] >= cut:
                out.append((str(ids[qi]), str(ids[j])))
    return out
