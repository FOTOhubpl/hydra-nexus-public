"""Per-family ablation on `sample+syn`: which decisions change when one family is left out (G3-FEATURES).

Measurement only; reuses eval/run.py (same corpus construction, Corpus, loo_jobs, run_folds, build with
drop={family}). For the full system and each of -VIS, -TXT, -INF, -ID, -NAR, -META it records
(1) leave-one-out rows whose decision or outcome differs from the full run, and
(2) campaign pairs linked in the full build (all incidents) but not in the ablated build, and vice versa.
Prints/writes incident ids only, never content.

    python -W ignore scripts/ablation_flips.py --out-dir DIR --tmp-root DIR --extract-workers 6 --workers 10
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import run as R  # noqa: E402
from eval import variants as V  # noqa: E402
from eval.backend import make_backend  # noqa: E402

CONFIGS = ["full"] + [f"-{f}" for f in R.FAMILIES]


def linked_pairs(member_of: dict) -> set[tuple[str, str]]:
    return {(a, b) for a, b in combinations(sorted(member_of), 2)
            if member_of[a] is not None and member_of[a] == member_of[b]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=R.NASK / "sample")
    ap.add_argument("--labels", type=Path, default=R.NASK / "labels" / "sample_labels.json")
    ap.add_argument("--variants", type=Path, default=R.NASK / "variants")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--tmp-root", type=Path, required=True)
    ap.add_argument("--extract-workers", type=int, default=6)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--examples", type=int, default=10)
    a = ap.parse_args()
    t0 = time.perf_counter()
    sample_rows = V.load_sample(a.sample, a.labels)
    R.CAMPAIGN_LABELS.update(r["label"] for r in sample_rows if r.get("label"))
    s2 = R.relabel(R.uniq_ids(R.relocate(json.loads((a.variants / "set-s2" / "manifest.json").read_text())
                                         ["incidents"], a.variants), "s2"), sample_rows)
    state = a.tmp_root / "main"
    primary = make_backend("hydra", state_dir=state, thresholds={})
    rows = sample_rows + s2
    R.parallel_extract(primary, rows, [], a.tmp_root, a.extract_workers)
    mixed = R.Corpus("sample+syn", R.refs_from_rows(rows), primary)
    R.register(mixed)
    R.CTX["backend"] = primary
    if a.workers > 1:
        R.start_pool(a.workers)
    all_ids = sorted(mixed.genes)
    jobs, owner = [], []
    for cfg in CONFIGS:
        drop = sorted({cfg[1:]}) if cfg != "full" else []
        jobs.append({"corpus": mixed.name, "build_ids": all_ids, "drop": drop, "metrics": True,
                     "all_members": True, "tag": f"flips:{cfg}", "stage": "build_ablation"})
        owner.append((cfg, "full"))
        for j in R.loo_jobs(mixed, frozenset(drop)):
            jobs.append(j)
            owner.append((cfg, "loo"))
    results = R.run_folds(jobs)
    R.stop_pool()
    per: dict = {}
    for cfg in CONFIGS:
        full = [r for (c, k), r in zip(owner, results, strict=True) if c == cfg and k == "full"][0]["metrics"]
        loo = {row["incident"]: row for (c, k), r in zip(owner, results, strict=True) if c == cfg and k == "loo"
               for row in r["rows"]}
        per[cfg] = {"metrics": {k: v for k, v in (full or {}).items() if k != "member_of"},
                    "pairs": linked_pairs((full or {}).get("member_of") or {}), "loo": loo}
    base = per["full"]
    out: dict = {"commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                                          cwd=R.EVAL_DIR.parent).stdout.strip(),
                 "corpus": {"name": "sample+syn", "n_incidents": len(all_ids), "n_labelled_loo": len(base["loo"]),
                            "full_linked_pairs": len(base["pairs"])},
                 "adv_f01": os.environ.get("HYDRA_ADV_F01", "default(on)"), "configs": {}}
    for cfg in CONFIGS[1:]:
        p = per[cfg]
        flips = []
        for iid, r0 in sorted(base["loo"].items()):
            r1 = p["loo"].get(iid)
            if r1 is None:
                flips.append({"incident": iid, "full": [r0["decision"], r0["outcome"]], "ablated": "missing"})
            elif (r0["decision"], r0["outcome"], r0["assigned_label"]) != (r1["decision"], r1["outcome"],
                                                                          r1["assigned_label"]):
                flips.append({"incident": iid, "full": [r0["decision"], r0["outcome"], r0["assigned_label"]],
                              "ablated": [r1["decision"], r1["outcome"], r1["assigned_label"]]})
        lost, gained = sorted(base["pairs"] - p["pairs"]), sorted(p["pairs"] - base["pairs"])
        lab = mixed.labels
        out["configs"][cfg] = {
            "metrics": p["metrics"], "loo_flips": flips, "pairs_lost": len(lost), "pairs_gained": len(gained),
            "pairs_lost_same_label": sum(1 for x, y in lost if lab.get(x) and lab.get(x) == lab.get(y)),
            "pairs_gained_same_label": sum(1 for x, y in gained if lab.get(x) and lab.get(x) == lab.get(y)),
            "examples_lost": [list(x) for x in lost[:a.examples]],
            "examples_gained": [list(x) for x in gained[:a.examples]]}
    out["full_metrics"] = base["metrics"]
    out["wall_s"] = round(time.perf_counter() - t0, 1)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "ablation_flips.json").write_text(json.dumps(out, indent=1))
    md = [f"Ablation flips on `sample+syn` ({len(all_ids)} incidents, {len(base['loo'])} labelled LOO items, "
          f"{len(base['pairs'])} linked pairs in the full build), commit `{out['commit'][:7]}`, "
          f"wall {out['wall_s']} s. Pairs lost/gained: linked in the full build but not without the family, and "
          "vice versa ('same-label' = both incidents carry the same campaign label, i.e. a lost true link or a "
          "gained true link).", "",
          "| Removed | LOO decisions changed | Campaign pairs lost (same-label) | Pairs gained (same-label) | "
          "Campaign F1 | Examples |", "|---|---:|---:|---:|---:|---|"]
    for cfg, c in out["configs"].items():
        ex = [f"{f['incident']}: {'/'.join(map(str, f['full'][:2]))} -> "
              f"{'/'.join(map(str, f['ablated'][:2])) if f['ablated'] != 'missing' else 'missing'}"
              for f in c["loo_flips"][:4]]
        ex += [f"lost {x}~{y}" for x, y in c["examples_lost"][:3]]
        ex += [f"gained {x}~{y}" for x, y in c["examples_gained"][:3]]
        md.append(f"| {cfg} | {len(c['loo_flips'])} | {c['pairs_lost']} ({c['pairs_lost_same_label']}) | "
                  f"{c['pairs_gained']} ({c['pairs_gained_same_label']}) | {c['metrics'].get('campaign_f1')} | "
                  f"{'; '.join(ex) or 'no change'} |")
    (a.out_dir / "ablation_flips.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
