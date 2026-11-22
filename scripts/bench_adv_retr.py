"""ADV-RETR bench: candidate recall of the retrieval union vs an all-pairs oracle on a read-only state copy.

usage: python scripts/bench_adv_retr.py --db <copy of hydra.sqlite> [--n 231] [--json out.json]

Per mode (`base` = today's retrieval, `adv` = HYDRA_ADV_RETR=1, `oracle` = every pair scored) the corpus is scored
and clustered with `pipeline.score_and_cluster` (single pass, default thresholds), as `hydra analyse` does after
gene extraction. Reported: candidate pairs, candidate recall of the oracle's linked / same_campaign pairs and of
labelled same-label pairs, campaign partitions identical to the oracle's, wall seconds of retrieval and of
score+cluster. `--n`: deterministic subset (blake2b order of the incident id). Reads the database read-only; writes
nothing but the JSON. Aggregates only, never gene values.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import sqlite3
import time
from itertools import combinations

from hydra import pipeline as PL
from hydra import retrieval as R
from hydra.synapse import Hypothesis, Thresholds


def load(db: str, n: int | None):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ids = sorted(r[0] for r in conn.execute("SELECT id FROM incidents"))
    if n:
        ids = sorted(sorted(ids, key=lambda i: hashlib.blake2b(i.encode(), digest_size=8).hexdigest())[:n])
    return PL.load_genomes(conn, ids)


def run_mode(genomes, mode: str) -> dict:
    os.environ.pop("HYDRA_ADV_RETR", None)
    if mode == "adv":
        os.environ["HYDRA_ADV_RETR"] = "1"
    orig = PL.retrieve
    if mode == "oracle":
        PL.retrieve = functools.partial(R.candidates, all_pairs_below=10**9)
    th = Thresholds()
    try:
        t0 = time.perf_counter()
        rarity, pairs, clustering, info = PL.score_and_cluster(genomes, th)
        wall = time.perf_counter() - t0
        t1 = time.perf_counter()
        cand = PL.retrieve(genomes, rarity)
        t_ret = time.perf_counter() - t1
    finally:
        PL.retrieve = orig
        os.environ.pop("HYDRA_ADV_RETR", None)
    by_src: dict[str, int] = {}
    for srcs in cand.values():
        for s in srcs:
            k = s.split(":", 1)[0]
            by_src[k] = by_src.get(k, 0) + 1
    only_adv = sum(1 for s in cand.values() if all(x.startswith(("lsh:", "tfidf:")) for x in s))
    return {"pairs": pairs, "clustering": clustering, "cand": set(cand), "wall_score_cluster_s": round(wall, 2),
            "wall_retrieval_s": round(t_ret, 3), "candidate_pairs": len(cand), "pairs_by_index": by_src,
            "pairs_only_adv_sources": only_adv}


def partition(cl) -> list[tuple[str, ...]]:
    return sorted(tuple(sorted(c.members)) for c in cl.campaigns)


def pairwise_f1(cl, labels: dict[str, str]) -> dict:
    own = {m: c.id for c in cl.campaigns for m in c.members}
    tp = fp = fn = 0
    for a, b in combinations(sorted(labels), 2):
        same_l = labels[a] == labels[b]
        same_p = own.get(a) is not None and own.get(a) == own.get(b)
        tp += same_l and same_p
        fp += same_p and not same_l
        fn += same_l and not same_p
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--modes", default="base,adv,oracle")
    args = ap.parse_args()
    t0 = time.perf_counter()
    genomes = load(args.db, args.n)
    out: dict = {"n_incidents": len(genomes), "all_pairs": len(genomes) * (len(genomes) - 1) // 2,
                 "load_s": round(time.perf_counter() - t0, 1), "modes": {}}
    labels = {i: g.label for i, g in genomes.items() if g.label}
    lab_pos = {(a, b) for a, b in combinations(sorted(labels), 2) if labels[a] == labels[b]}
    res = {m: run_mode(genomes, m) for m in args.modes.split(",")}
    th = Thresholds()
    ora = res.get("oracle")
    if ora:
        linked = {k for k, pe in ora["pairs"].items() if PL.is_linked(pe, th)}
        same = {k for k, pe in ora["pairs"].items() if pe.score.hypothesis == Hypothesis.SAME_CAMPAIGN}
        out["oracle_linked_pairs"], out["oracle_same_campaign_pairs"] = len(linked), len(same)
    out["labelled_same_label_pairs"] = len(lab_pos)
    for m, r in res.items():
        row = {k: r[k] for k in ("candidate_pairs", "pairs_by_index", "pairs_only_adv_sources",
                                 "wall_retrieval_s", "wall_score_cluster_s")}
        row["labelled_recall"] = round(len(lab_pos & r["cand"]) / len(lab_pos), 4) if lab_pos else None
        row["campaign_pairwise_vs_labels"] = pairwise_f1(r["clustering"], labels)
        row["campaigns_multi"] = sum(1 for c in r["clustering"].campaigns if len(c.members) > 1)
        if ora:
            row["recall_oracle_linked"] = round(len(linked & r["cand"]) / len(linked), 4) if linked else None
            row["recall_oracle_same_campaign"] = round(len(same & r["cand"]) / len(same), 4) if same else None
            row["missed_linked"] = len(linked - r["cand"])
            row["partition_equal_oracle"] = partition(r["clustering"]) == partition(ora["clustering"])
            # pairs the mode links that the oracle also links with the same hypothesis (identical scoring)
            row["hypothesis_mismatch_vs_oracle"] = sum(
                1 for k, pe in r["pairs"].items() if pe.score.hypothesis != ora["pairs"][k].score.hypothesis)
        out["modes"][m] = row
    if "base" in res and "adv" in res:
        out["partition_equal_base_adv"] = partition(res["base"]["clustering"]) == partition(res["adv"]["clustering"])
    out["total_s"] = round(time.perf_counter() - t0, 1)
    txt = json.dumps(out, indent=2, sort_keys=True)
    print(txt)
    if args.json:
        with open(args.json, "w") as f:
            f.write(txt)


if __name__ == "__main__":
    main()
