"""KONTRA K1 measurement on a SYNTHETIC corpus in a temporary state (no organiser data, no shared state).

Corpus: the spine corpus of tests/core/test_pipeline.py (16 incidents, campaigns A and B in one group via a shared
SDK build, fillers) analysed with its two test-only extractors t_page / t_vec, plus synthetic new items assigned
through `pipeline.assign_new` (assign / kit / new pattern / two byte-identical copies / copied creative / mixed
indicators). The e2e corpus (tests/e2e/corpus.py) needs the real extractors and local models and is not used here.

Then `kontra.run_experiment` runs on every assigned new item and on every corpus member (the product path is
leave-one-out for members). Aggregates are printed as a table and written as JSON.

Optional replay (`--replay`, on by default): for each incident and each family F in {INF, TXT, VIS} whose removal
changes the full-information decision, the genes of F are hidden; a simulator holds them and reveals, per HUNT
action, only the hidden genes of the families in `kontra.SOURCE_FAMILIES[action.source]` (assumed mapping). The
planner (`kontra.next_observations`) sees only the revealed query genome and the campaigns. Steps and cost until the
decision equals the full-information decision are compared with the fixed catalogue order of `hunt.actions.ACTIONS`.
The analyst action is excluded from both orders (the simulator has no analyst).

    uv run python scripts/kontra_measure.py [--out /tmp/kontra_measure.json] [--keep]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

for k, v in {"HYDRA_OFFLINE": "1", "HYDRA_EXTRACT_WORKERS": "2", "HYDRA_TORCH_THREADS": "2", "HYDRA_OCR_WORKERS": "1",
             "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "HYDRA_API_WARM": "0", "HYDRA_LEXICON_DIR": "none",
             "HYDRA_BACKGROUND_PRIOR": "0", "HYDRA_LLM": "0"}.items():
    os.environ[k] = v

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CLASSES = ("observation_uncertainty", "reuse_hypothesis", "diagnostic_stress", "competitor_check")
REVEAL_FAMILIES = ("INF", "TXT", "VIS")


def png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def summary(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 2), "median": round(statistics.median(xs), 2),
            "p95": round(pct(xs, 0.95), 2), "max": round(max(xs), 2)}


def outcome(res) -> tuple[str, str | None]:
    a = res.assignment
    return str(a.decision), a.campaign_id


def replay(inc_id: str, settings) -> list[dict]:
    """Hidden-family replay for one incident (see module docstring)."""
    from hydra.entropy import usable
    from hydra.hunt.actions import ACTIONS, COST
    from hydra.kontra import SOURCE_FAMILIES, _fam, _genome_without, freeze, hypotheses, next_observations, rescore

    ctx = freeze(inc_id, settings)
    full = ctx.genome
    target = outcome(rescore(ctx))
    by_id = {a.id: a for a in ACTIONS if a.source != "analyst"}
    out = []
    for fam in REVEAL_FAMILIES:
        hidden = frozenset(i for i, g in enumerate(full.genes) if usable(g) and _fam(g) == fam)
        hidden_out = outcome(rescore(ctx, drop=hidden)) if hidden else target
        if hidden_out == target:
            continue

        def reveal(drop: frozenset[int], action_id: str) -> frozenset[int]:
            fams = set(SOURCE_FAMILIES.get(by_id[action_id].source, []))
            return frozenset(i for i in drop if _fam(full.genes[i]) not in fams)

        def run(policy: str, hidden: frozenset[int] = hidden) -> dict:
            drop, taken, cost = hidden, [], 0
            while len(taken) < len(by_id):
                if policy == "planner":
                    ctx.genome = _genome_without(full, drop)  # the planner sees only the revealed genome
                    try:
                        base = rescore(ctx)
                        nxt = next_observations(ctx, base, hypotheses(ctx, base, {}))
                    finally:
                        ctx.genome = full
                    act = next(r["action"] for r in nxt["ranking"] if r["action"] in by_id and r["action"] not in taken)
                else:
                    act = next(a for a in by_id if a not in taken)
                taken.append(act)
                cost += COST[by_id[act].cost]
                drop = reveal(drop, act)
                if outcome(rescore(ctx, drop=drop)) == target:
                    return {"resolved": True, "steps": len(taken), "cost": cost, "actions": taken}
            return {"resolved": False, "steps": len(taken), "cost": cost, "actions": taken}

        out.append({"incident": inc_id, "hidden_family": fam, "target": list(target),
                    "hidden_outcome": list(hidden_out), "planner": run("planner"), "fixed": run("fixed")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--keep", action="store_true", help="keep the temp root")
    ap.add_argument("--no-replay", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="kontra-measure-"))
    os.environ["HYDRA_STATE_DIR"] = str(tmp / "state")
    os.environ["HYDRA_CACHE_DIR"] = str(tmp / "cache")
    from hydra.config import Settings
    from hydra.contracts import Family
    from hydra.genome import base as gbase
    from hydra.kontra import run_experiment
    from hydra.pipeline import analyse, assign_new
    from tests.core.test_pipeline import PX, corpus, page, t_page, t_vec

    settings = Settings(root=tmp, data_dir=tmp / "data", models_dir=tmp / "models", cache_dir=tmp / "cache",
                        state_dir=tmp / "state", max_file_mb=5)
    settings.ensure_dirs()
    for name, fn, fam in (("t_page", t_page, Family.INF), ("t_vec", t_vec, Family.VIS)):
        if name not in gbase.REGISTRY:
            gbase.register(name, family=fam)(fn)
    only = ["t_page", "t_vec"]
    try:
        t0 = time.perf_counter()
        ds = corpus(tmp, lambda c=(200, 30, 30): png(c))
        analyse([ds], settings, only=only)
        analyse_s = time.perf_counter() - t0

        new = tmp / "new"
        items = {
            "assign_a": ("evil-a.example", "+48 700 100 100" + PX, None),
            "kit_sdk": ("fresh.example", '<i data-sdk="aio-sdk-1.0.28-x"></i>', None),
            "new_none": ("other.example", "", None),
            "copy_1": ("copycat.example", '<i data-acct="cc-77"></i> +48 511 222 333', None),
            "copy_2": ("copycat.example", '<i data-acct="cc-77"></i> +48 511 222 333', None),
            "creative_reuse": ("unrelated-z.example", "", (10, 200, 30)),  # same creative as base incident 6
            "mixed_ab": ("evil-a.example", "+48 600 100 200", None),        # host of A, phone of B
            "assign_b": ("shop-b.example", "+48 600 100 200", None),
        }
        assigned = {}
        t0 = time.perf_counter()
        for name, (host, extra, color) in items.items():
            page(new / name, host, extra)
            if color is not None:
                (new / name / "Zrzut ekranu 2026-10-02 101010.png").write_bytes(png(color))
            _, a = assign_new(new / name, settings, only=only)
            assigned[name] = a
        assign_s = time.perf_counter() - t0

        from contextlib import closing

        from hydra.db import connect

        new_ids = {a.incident_id for a in assigned.values()}
        name_of = {a.incident_id: n for n, a in assigned.items()}
        with closing(connect(settings.db_path, readonly=True)) as conn:
            all_ids = [r["id"] for r in conn.execute("SELECT id FROM incidents ORDER BY id")]
        members = [i for i in all_ids if i not in new_ids]

        exps = []
        for inc in [*(a.incident_id for a in assigned.values()), *members]:
            exps.append(("new" if inc in new_ids else "member", run_experiment(inc, settings)))

        per_class = {c: [0, 0] for c in CLASSES}  # [incidents with >=1 flip, incidents with >=1 scenario]
        any_flip = 0
        flip_found, flip_sizes, recomps, inc_ms, rec_ms, matches = 0, [], [], [], [], []
        decisions: dict[str, int] = {}
        rows_out = []
        for origin, e in exps:
            b = e["baseline"]
            decisions[b["decision"]] = decisions.get(b["decision"], 0) + 1
            scen = [r for r in e["scenarios"] if r["type"] != "baseline"]
            flipped = [r for r in scen if r["decision_changed"] or r["campaign_changed"]]
            any_flip += bool(flipped)
            for c in CLASSES:
                rs = [r for r in scen if r["class"] == c]
                if rs:
                    per_class[c][1] += 1
                    per_class[c][0] += any(r["decision_changed"] or r["campaign_changed"] for r in rs)
            fs = e["flip_set"]
            if fs["found"]:
                flip_found += 1
                flip_sizes.append(len(fs["groups"]))
            recomps.append(e["stats"]["recomputations"])
            inc_ms.append(e["stats"]["runtime_ms_total"])
            rec_ms.extend(r["runtime_ms"] for r in e["scenarios"])
            matches.append(b["matches_stored"])
            rows_out.append({"incident": e["incident_id"], "origin": name_of.get(e["incident_id"], origin),
                             "decision": b["decision"],
                             "campaign": b["campaign_id"], "matches_stored": b["matches_stored"],
                             "scenarios": len(scen), "flips": len(flipped), "flip_set": fs["groups"] if fs["found"]
                             else None, "recomputations": e["stats"]["recomputations"],
                             "stability": e["stats"]["scenario_stability_fraction"],
                             "next_top": e["next_observations"]["ranking"][0]["action"],
                             "ms": e["stats"]["runtime_ms_total"]})
        n = len(exps)
        result = {
            "corpus": "synthetic spine corpus (tests/core/test_pipeline.py) + 8 synthetic new items; extractors"
                      " t_page/t_vec", "analyse_s": round(analyse_s, 2), "assign_new_s": round(assign_s, 2),
            "incidents": n, "new_items": len(new_ids), "corpus_members": len(members),
            "baseline_decisions": decisions,
            "baseline_matches_stored": {"true": matches.count(True), "false": matches.count(False),
                                        "none": matches.count(None)},
            "incidents_with_any_flip": any_flip,
            "flip_by_class": {c: {"incidents_with_flip": v[0], "incidents_with_class": v[1],
                                  "fraction": round(v[0] / v[1], 3) if v[1] else None} for c, v in per_class.items()},
            "flip_set": {"found": flip_found, "rate": round(flip_found / n, 3),
                         "mean_size": round(statistics.fmean(flip_sizes), 2) if flip_sizes else None},
            "recomputations_per_incident": summary([float(x) for x in recomps]),
            "latency_ms_per_incident": summary(inc_ms),
            "latency_ms_per_recomputation": summary(rec_ms),
            "per_incident": rows_out,
        }
        if not args.no_replay:
            t0 = time.perf_counter()
            cases = [c for inc in all_ids for c in replay(inc, settings)]
            agg = {}
            for pol in ("planner", "fixed"):
                ok = [c[pol] for c in cases if c[pol]["resolved"]]
                agg[pol] = {"resolved": len(ok), "mean_steps": round(statistics.fmean(x["steps"] for x in ok), 3)
                            if ok else None, "mean_cost": round(statistics.fmean(x["cost"] for x in ok), 3)
                            if ok else None}
            nontriv = [c for c in cases if c["hidden_outcome"][0] != "abstain"]
            agg["non_abstain_hidden_cases"] = {
                "cases": len(nontriv),
                **{pol: {"resolved": sum(c[pol]["resolved"] for c in nontriv),
                         "steps": [c[pol]["steps"] for c in nontriv]} for pol in ("planner", "fixed")}}
            both = [c for c in cases if c["planner"]["resolved"] and c["fixed"]["resolved"]]
            agg.update(cases=len(cases), seconds=round(time.perf_counter() - t0, 2),
                       planner_fewer_steps=sum(c["planner"]["steps"] < c["fixed"]["steps"] for c in both),
                       equal_steps=sum(c["planner"]["steps"] == c["fixed"]["steps"] for c in both),
                       planner_more_steps=sum(c["planner"]["steps"] > c["fixed"]["steps"] for c in both),
                       by_hidden_family={f: sum(c["hidden_family"] == f for c in cases) for f in REVEAL_FAMILIES},
                       fixed_order=[a for a in __import__("hydra.hunt.actions", fromlist=["ACTIONS"]).BY_ID
                                    if a != "ask_analyst_pair"],
                       cases_detail=cases)
            result["replay"] = agg
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"{'incident':12} {'origin':14} {'decision':13} {'campaign':10} {'stored=':7} {'scen':>4} {'flips':>5}"
          f" {'recomp':>6} {'stab':>5} {'ms':>8}  flip_set / next")
    for r in result["per_incident"]:
        print(f"{r['incident']:12} {r['origin']:14} {r['decision']:13} {str(r['campaign']):10}"
              f" {str(r['matches_stored']):7} {r['scenarios']:>4} {r['flips']:>5} {r['recomputations']:>6}"
              f" {str(r['stability']):>5} {r['ms']:>8.1f}  {r['flip_set']} / {r['next_top']}")
    brief = {k: v for k, v in result.items() if k != "per_incident"}
    if "replay" in brief:
        brief["replay"] = {k: v for k, v in brief["replay"].items() if k != "cases_detail"}
    print(json.dumps(brief, indent=1, ensure_ascii=False))
    if args.out:
        args.out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
