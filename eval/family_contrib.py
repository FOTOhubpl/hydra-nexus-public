"""Per-family contribution per hidden_sim variant type (EVAL-EXTRA, 2026-11-22). Measurement only.

For every item of a sharded hidden_sim run (`assignq/run_shards.sh` layout: out-k/assignments.json, state-k),
re-decide the stored genes through the product path `pipeline.assign_stored` once with every family and once
with each family left out. Leaving a family out uses the existing leave-one-family-out switch of
`synapse.score_pair(drop=...)`, bound for the duration of the replay in the assignment module only; the base
campaigns (built with every family) are unchanged, so this measures what each family contributes to the
assignment of a new item, not to the clustering.

    python -m eval.family_contrib --run <run dir> --truth <sim>/truth.csv --json out.json [--workers 4]
Families at c7f4817 are `contracts.Family` (VIS, TXT, INF, ID, NAR, META); audio, video, provenance and
deepfake genes belong to these families (no separate family to drop). Prints aggregates only.
"""

from __future__ import annotations

import argparse
import functools
import json
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

CONFIGS = ["all", "-VIS", "-TXT", "-INF", "-ID", "-NAR", "-META"]


def summarise(judged: dict[str, list[dict]]) -> dict:
    """config -> variant -> {n, ok, false_assign, rejected}; plus per-config totals. `judged` rows carry
    `variant`, `expect` ('new' or 'known:...') and `outcome` (eval.hidden_sim.judge)."""
    out: dict = {}
    for cfg, rows in judged.items():
        per: dict[str, Counter] = defaultdict(Counter)
        for j in rows:
            c = per[j["variant"]]
            c["n"] += 1
            c["ok"] += j["outcome"] in ("ok", "ok_new")
            c["false_assign"] += j["outcome"] in ("false_assign", "wrong_campaign")
            c["abstain"] += j["outcome"] == "abstain"
            c["missed_new"] += j["outcome"] == "missed_new"
        pos = [j for j in rows if j["expect"] != "new"]
        neg = [j for j in rows if j["expect"] == "new"]
        out[cfg] = {"per_variant": {v: dict(c) for v, c in sorted(per.items())},
                    "positives": len(pos), "assign_ok": sum(j["outcome"] == "ok" for j in pos),
                    "assign_accuracy": round(sum(j["outcome"] == "ok" for j in pos) / max(1, len(pos)), 3),
                    "negatives": len(neg), "negatives_rejected": sum(j["outcome"] == "ok_new" for j in neg),
                    "negatives_abstain": sum(j["outcome"] == "abstain" for j in neg),
                    "false_assigns": sum(j["outcome"] in ("false_assign", "wrong_campaign") for j in rows)}
    return out


def _shard(job: tuple[str, int, str]) -> tuple[str, list[dict]]:
    """Re-decide one shard under one config (separate process: the assignment module binding is local)."""
    run_dir, k, cfg = Path(job[0]), job[1], job[2]
    import hydra.assign as HA
    from hydra.synapse import score_pair

    # pool processes are reused across jobs: always (re)bind, the original for "all"
    HA.score_pair = score_pair if cfg == "all" else functools.partial(score_pair, drop=frozenset({cfg[1:]}))
    from eval.hidden_sim import rescore

    rows = json.loads((run_dir / f"out-{k}" / "assignments.json").read_text(encoding="utf-8"))
    return cfg, rescore(run_dir / f"state-{k}", rows)


def main() -> None:
    from eval.hidden_sim import campaign_of, judge, load_truth

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--configs", nargs="+", default=CONFIGS)
    a = ap.parse_args()
    truth = load_truth(a.truth)
    jobs = [(str(a.run), k, cfg) for cfg in a.configs for k in range(a.shards)]
    judged: dict[str, list[dict]] = defaultdict(list)
    members = {k: campaign_of(a.run / f"state-{k}")[1] for k in range(a.shards)}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for (cfg, rows), (_r, k, _c) in zip(ex.map(_shard, jobs), jobs, strict=True):
            names = {r["item"] for r in rows}
            judged[cfg] += judge(rows, {i: t for i, t in truth.items() if i in names}, members[k])
    summary = summarise(judged)
    variants = sorted({v for s in summary.values() for v in s["per_variant"]})
    print(f"{'variant':<18}" + "".join(f"{c:>9}" for c in a.configs) + "   (ok / n)")
    for v in variants:
        cells = [summary[c]["per_variant"].get(v, {}) for c in a.configs]
        print(f"{v:<18}" + "".join(f"{x.get('ok', 0):>5}/{x.get('n', 0):<3}" for x in cells))
    for c in a.configs:
        s = summary[c]
        print(f"{c:<6} assign {s['assign_ok']}/{s['positives']} ({s['assign_accuracy']})  negatives rejected"
              f" {s['negatives_rejected']}/{s['negatives']} (abstain {s['negatives_abstain']})"
              f"  false assigns {s['false_assigns']}")
    if a.json:
        a.json.write_text(json.dumps(summary, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
