"""F02 feature economics on a SYNTHETIC corpus (no organiser data): per gene family and kind, how often it is
observed, how many campaign-level bits it carries in true vs false links, what it costs to extract and how often its
extractor reports unavailable / failed.

    . scripts/env.sh
    uv run python scripts/feature_economics.py --root "$(mktemp -d /tmp/hydra-f02.XXXXXX)" [--members 4] [--json out]

Corpus: the e2e generator `tests/e2e/corpus.py` (campaign A investment lure: HTML + PNG + HAR; B fake shop: HTML/ZIP +
PDF + chat; C parcel surcharge: PNG + MP4 + chat; D energy refund: HTML + PNG + chat; 8 unrelated benign negatives:
HTML + photo PNG). Ground truth = the generator's campaign letter; every negative is its own label. The corpus is
deterministic (no RNG beyond the generator's fixed seeds), so its sha256 below identifies it.

Run: one in-process `hydra.pipeline.analyse` with every registered extractor in a fresh state + cache under --root
(gene CAS off, so every extractor really runs). Then EVERY incident pair (not only retrieval candidates) is rescored
with `hydra.synapse.score_pair` on the run's genomes and rarity (same Thresholds, unlimited reason rows) so the
per-family / per-kind bits of true links are measured, not sampled.

Measures
- frequency: share of incidents holding >= 1 observed gene of the family / kind;
- campaign bits per family: `PairEval.campaign_by_family` (after group and family caps, exactly what the decision
  uses), summed over true pairs (same synthetic campaign) and over false pairs (different labels);
- per kind: raw reason bits (before family caps) of campaign-level reason rows (`synapse.level`), true vs false;
- cost: per extractor `metrics.extractors[*].run_seconds` of the run (sum over incidents; parallel workers measure
  their own compute), and its genes by status observed / unavailable / failed;
- link outcome: hypothesis of every pair vs truth (pairs with same_campaign).

Writes JSON (stdout summary + --json file); docs/release/FEATURE_ECONOMICS.md is written from it by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_corpus(ds: Path, members: int) -> dict[str, str]:
    """Folder name -> truth label."""
    sys.path.insert(0, str(ROOT))
    from tests.e2e import corpus as c

    truth: dict[str, str] = {}
    for n in range(1, members + 1):
        c._campaign_a(ds / f"a{n}", n)
        c._campaign_b(ds / f"b{n}", n, zipped=(n == members))
        c._campaign_c(ds / f"c{n}", n)
        truth.update({f"a{n}": "A", f"b{n}": "B", f"c{n}": "C"})
    for n in range(1, min(members, 3) + 1):
        c._campaign_d(ds / f"d{n}", n)
        truth[f"d{n}"] = "D"
    for i in range(1, len(c.NEG) + 1):
        c._negative(ds / f"n{i}", i)
        truth[f"n{i}"] = f"NEG{i}"
    return truth


def corpus_sha(ds: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(ds.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(ds).as_posix().encode() + b"|")
            # ZIP and MP4 carry timestamps/encoder noise; hash the generator inputs instead of their bytes
            h.update(p.read_bytes() if p.suffix.lower() not in (".zip", ".mp4") else p.suffix.encode())
    return h.hexdigest()[:16]


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True, help="fresh temp dir (state, cache, corpus)")
    ap.add_argument("--members", type=int, default=4, help="incidents per campaign A/B/C (1-9; D gets min(3, n))")
    ap.add_argument("--json", type=Path, help="write the full result here")
    args = ap.parse_args()
    root = args.root.expanduser().resolve()
    home_state = Path("~/HydraNexus/state").expanduser().resolve()
    if root == home_state or home_state in root.parents:
        sys.exit("refusing to run inside the main state")
    os.environ.setdefault("HYDRA_OFFLINE", "1")
    os.environ["HYDRA_ENRICH"] = "0"
    os.environ["HYDRA_GENE_CAS"] = "0"

    from hydra.config import Settings
    from hydra.genome import base
    from hydra.pipeline import analyse
    from hydra.synapse import Thresholds, level, score_pair

    ds = root / "ds"
    truth_by_folder = build_corpus(ds, args.members)
    s = Settings(root=root, data_dir=root / "data", cache_dir=root / "cache", state_dir=root / "state",
                 enrich=False, offline=True)
    s.ensure_dirs()
    t0 = time.perf_counter()
    res = analyse([ds], s, use_cache=False)
    wall = time.perf_counter() - t0

    def folder(iid: str) -> str:
        return iid.split("-", 1)[1] if "-" in iid else iid

    truth = {iid: truth_by_folder.get(folder(iid), f"?{iid}") for iid in res.genomes}
    unknown = [i for i, lab in truth.items() if lab.startswith("?")]
    th = Thresholds(max_reasons=10_000)
    tag_name = {ex.tag: n for n, ex in base.REGISTRY.items()}

    # frequency and level per (family, kind), from observed genes
    fam_inc: dict[str, set[str]] = defaultdict(set)
    kind_inc: dict[tuple[str, str], set[str]] = defaultdict(set)
    kind_level: dict[tuple[str, str], Counter] = defaultdict(Counter)
    kind_extractor: dict[tuple[str, str], Counter] = defaultdict(Counter)
    ex_inc: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for iid, g in res.genomes.items():
        for x in g.genes:
            ex_inc[tag_name.get(x.extractor or "", x.extractor or "?")][str(x.status)].add(iid)
            if str(x.status) != "observed":
                continue
            fk = (str(x.family), x.kind)
            fam_inc[fk[0]].add(iid)
            kind_inc[fk].add(iid)
            kind_level[fk][level(x)] += 1
            kind_extractor[fk][tag_name.get(x.extractor or "", x.extractor or "?")] += 1

    ids = sorted(res.genomes)
    n_true = n_false = 0
    fam_bits = {"true": Counter(), "false": Counter()}
    fam_all = {"true": Counter(), "false": Counter()}
    lvl_tot = {"true": Counter(), "false": Counter()}
    fam_pairs = {"true": Counter(), "false": Counter()}
    kind_bits = {"true": Counter(), "false": Counter()}
    hyp_tab: Counter = Counter()
    hyp_true_by_label: Counter = Counter()
    dup_bits = {"true": 0.0, "false": 0.0}
    camp_bits_total = {"true": 0.0, "false": 0.0}
    for a, b in combinations(ids, 2):
        side = "true" if truth[a] == truth[b] else "false"
        n_true += side == "true"
        n_false += side == "false"
        pe = score_pair(res.genomes[a], res.genomes[b], res.rarity, th)
        hyp_tab[(side, str(pe.score.hypothesis))] += 1
        if side == "true":
            hyp_true_by_label[(truth[a], str(pe.score.hypothesis))] += 1
        dup_bits[side] += max(0.0, pe.campaign_bits - sum(pe.campaign_by_family.values()))
        camp_bits_total[side] += pe.campaign_bits
        lvl_tot[side].update({"campaign": pe.campaign_bits, "kit": pe.kit_bits, "asset": pe.asset_bits})
        for f, v in pe.score.family_bits.items():
            if v > 0:
                fam_all[side][f] += v
        for f, v in pe.campaign_by_family.items():
            if v > 0:
                fam_bits[side][f] += v
                fam_pairs[side][f] += 1
        for r in pe.score.reasons:
            fk = (str(r.family), r.kind)
            lv = kind_level.get(fk)
            if r.bits > 0 and lv and lv.most_common(1)[0][0] == "campaign":
                kind_bits[side][fk] += r.bits

    ex = res.metrics.get("extractors") or {}
    per_inc: dict[str, list[float]] = defaultdict(list)
    for st in (res.metrics.get("extract") or {}).values():
        for n, v in (st.get("extractor_seconds") or {}).items():
            per_inc[n].append(float(v))

    def pct(xs: list[float], q: float) -> float | None:
        return round(sorted(xs)[min(len(xs) - 1, int(q * len(xs)))], 4) if xs else None

    fam_of_ex = {n: str(e.family) for n, e in base.REGISTRY.items()}
    n_inc = len(ids)
    out = {
        "synthetic": True,
        "commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--", "hydra")),
        "host": platform.node(), "platform": f"{sys.platform}-{platform.machine()}",
        "date": time.strftime("%Y-%m-%d %H:%M %Z"),
        "env": {k: os.environ.get(k) for k in ("HYDRA_EXTRACT_WORKERS", "HYDRA_TORCH_THREADS", "HYDRA_OCR_WORKERS",
                                               "OMP_NUM_THREADS", "HYDRA_MODELS_DIR")},
        "corpus": {"generator": "tests/e2e/corpus.py", "members_per_campaign": args.members,
                   "sha256_16": corpus_sha(ds), "incidents": n_inc,
                   "labels": dict(Counter(truth.values())), "unmapped": unknown},
        "run": {"run_id": res.run_id, "wall_s": round(wall, 1), "timings_s": res.metrics.get("timings_s"),
                "campaigns": res.metrics.get("campaigns"),
                "multi_member_campaigns": res.metrics.get("multi_member_campaigns")},
        "pairs": {"true": n_true, "false": n_false,
                  "hypothesis": {f"{k[0]}:{k[1]}": v for k, v in sorted(hyp_tab.items())},
                  "true_hypothesis_by_label": {f"{k[0]}:{k[1]}": v for k, v in sorted(hyp_true_by_label.items())},
                  "campaign_bits_total": {k: round(v, 2) for k, v in camp_bits_total.items()},
                  "duplicate_file_bits": {k: round(v, 2) for k, v in dup_bits.items()},
                  "level_bits_total": {k: {lv: round(v, 2) for lv, v in c.items()} for k, c in lvl_tot.items()}},
        "families": {
            f: {"incident_share": round(len(fam_inc.get(f, ())) / n_inc, 3),
                "true_bits": round(fam_bits["true"][f], 2), "false_bits": round(fam_bits["false"][f], 2),
                "true_bits_share": round(fam_bits["true"][f] / camp_bits_total["true"], 3)
                if camp_bits_total["true"] else None,
                "true_pairs_with_bits": fam_pairs["true"][f], "false_pairs_with_bits": fam_pairs["false"][f],
                "all_level_true_bits": round(fam_all["true"][f], 2),
                "all_level_false_bits": round(fam_all["false"][f], 2)}
            for f in sorted(set(fam_inc) | set(fam_bits["true"]) | set(fam_bits["false"]))},
        "kinds": {
            f"{fk[0]}:{fk[1]}": {"incident_share": round(len(kind_inc[fk]) / n_inc, 3),
                                 "level": kind_level[fk].most_common(1)[0][0],
                                 "extractor": kind_extractor[fk].most_common(1)[0][0],
                                 "true_raw_bits": round(kind_bits["true"][fk], 2),
                                 "false_raw_bits": round(kind_bits["false"][fk], 2)}
            for fk in sorted(kind_inc)},
        "extractors": {
            n: {"family": fam_of_ex.get(n, "?"), "run_seconds": r.get("run_seconds"),
                "s_per_incident": round(float(r.get("run_seconds") or 0) / max(1, r.get("incidents_run") or 0), 4),
                "p50_s": pct(per_inc.get(n, []), 0.5), "max_s": pct(per_inc.get(n, []), 1.0),
                "incidents_run": r.get("incidents_run"), "observed": r.get("observed"),
                "unavailable": r.get("unavailable"), "failed": r.get("failed"),
                "unavailable_rate": round(r["unavailable"] / tot, 3) if (tot := sum(
                    r.get(k) or 0 for k in ("observed", "unavailable", "failed"))) else None,
                "failed_rate": round(r["failed"] / tot, 3) if tot else None,
                "incidents_by_status": {k: len(v) for k, v in sorted(ex_inc.get(n, {}).items())}}
            for n, r in ex.items()},
    }
    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
