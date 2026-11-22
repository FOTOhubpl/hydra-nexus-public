"""ADV-RETR proof: the branch's `hydra.retrieval` (flag off) returns exactly the reference version's candidates,
key order and source sets included, in corpus, per-query and query mode, and times both.

usage: python scripts/prove_retr_identical.py --db <copy of hydra.sqlite> --ref-file <old retrieval.py> [--n N]
`--ref-file`: e.g. `git show origin/integration:hydra/retrieval.py > /tmp/retrieval_ref.py`. Read-only DB access;
prints aggregates only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from bench_adv_retr import load  # noqa: E402

from hydra import pipeline as PL  # noqa: E402
from hydra import retrieval as NEW  # noqa: E402
from hydra.entropy import duplicate_units  # noqa: E402

NEW_SCOPE_KEYS = ("knn_above_floor", "knn_beyond_k", "knn_beyond_k_total")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--ref-file", required=True)
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--queries", type=int, default=60)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--adv", action="store_true")
    ap.add_argument("--no-per-query", action="store_true", help="skip the per-query passes (large corpora)")
    args = ap.parse_args()
    os.environ.pop("HYDRA_ADV_RETR", None)
    spec = importlib.util.spec_from_file_location("retrieval_ref", args.ref_file)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    g = load(args.db, args.n)
    print("loaded", len(g), file=sys.stderr, flush=True)
    r = PL.rarity_for(g, duplicate_units({i: x.genes for i, x in g.items()}))
    out: dict = {"n_incidents": len(g)}
    times: dict[str, list[float]] = {"ref": [], "new": []}
    items = {}
    for _rep in range(args.repeat):  # alternating, min of `repeat` (shared host)
        for name, mod in (("ref", old), ("new", NEW)):
            t = time.perf_counter()
            c = mod.candidates(g, r)
            times[name].append(time.perf_counter() - t)
            items[name] = list(c.items())
    out["corpus_identical_ordered"] = items["ref"] == items["new"]
    out["corpus_pairs"] = len(items["new"])
    out["corpus_s_min"] = {k: round(min(v), 3) for k, v in times.items()}
    out["corpus_s_median"] = {k: round(sorted(v)[len(v) // 2], 3) for k, v in times.items()}
    print("corpus mode", out, file=sys.stderr, flush=True)
    if not args.no_per_query:
        t = time.perf_counter()
        pq_old = old.candidates_per_query(g, r)
        t1 = time.perf_counter()
        pq_new = NEW.candidates_per_query(g, r)
        t2 = time.perf_counter()
        out["per_query_identical_ordered"] = all(list(pq_old[i].items()) == list(pq_new[i].items()) for i in g)
        out["per_query_s"] = {"ref": round(t1 - t, 3), "new": round(t2 - t1, 3)}
        del pq_old, pq_new
    qs = sorted(g)[:: max(1, len(g) // args.queries)]
    out["query_mode_identical_ordered"] = all(
        list(old.candidates(g, r, queries=[q]).items()) == list(NEW.candidates(g, r, queries=[q]).items())
        for q in qs)
    out["query_mode_checked"] = len(qs)
    t = time.perf_counter()
    scopes = [NEW.query_scope(g, r, q) for q in qs]  # CR-UI-FINAL-5 telemetry on real queries
    out["scope_s_mean"] = round((time.perf_counter() - t) / len(qs), 3)
    t = time.perf_counter()
    for q in qs:
        old.query_scope(g, r, q)
    out["scope_ref_s_mean"] = round((time.perf_counter() - t) / len(qs), 3)
    out["scope_queries_with_knn_overflow"] = sum(1 for s in scopes if s.get("knn_beyond_k_total"))
    out["scope_knn_beyond_k_sum"] = sum(s.get("knn_beyond_k_total", 0) for s in scopes)
    out["scope_knn_beyond_k_max"] = max((s.get("knn_beyond_k_total", 0) for s in scopes), default=0)
    out["scope_old_keys_unchanged"] = all(
        {k: v for k, v in s.items() if k not in NEW_SCOPE_KEYS} == old.query_scope(g, r, q)
        for s, q in zip(scopes, qs, strict=True))
    if args.adv:  # extra sources at this scale: retrieval time and pairs only they propose (no scoring)
        os.environ["HYDRA_ADV_RETR"] = "1"
        t = time.perf_counter()
        c = NEW.candidates(g, r)
        out["adv_corpus_s"] = round(time.perf_counter() - t, 3)
        base = dict(items["new"])
        by: dict[str, int] = {}
        for k, src in c.items():
            for x in src:
                if x.startswith(("lsh:", "tfidf:")):
                    by[x] = by.get(x, 0) + 1
                    if k not in base:
                        by[x + ":new_pair"] = by.get(x + ":new_pair", 0) + 1
        out["adv_pairs"] = len(c)
        out["adv_new_pairs"] = len(set(c) - set(base))
        out["adv_by_source"] = by
        out["adv_base_pairs_unchanged"] = all(base[k] <= c[k] for k in base)
        print("adv corpus", out["adv_corpus_s"], file=sys.stderr, flush=True)
        pq = None if args.no_per_query else NEW.candidates_per_query(g, r)
        t = time.perf_counter()
        got = [NEW.candidates(g, r, queries=[q]) for q in qs]
        out["adv_query_s_mean"] = round((time.perf_counter() - t) / len(qs), 3)
        if pq is not None:
            out["adv_query_equals_per_query"] = all(pq[q] == c_q for q, c_q in zip(qs, got, strict=True))
        os.environ.pop("HYDRA_ADV_RETR", None)
        t = time.perf_counter()
        for q in qs:
            NEW.candidates(g, r, queries=[q])
        out["base_query_s_mean"] = round((time.perf_counter() - t) / len(qs), 3)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
