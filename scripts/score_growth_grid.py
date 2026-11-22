"""SCORE-GROWTH (ASTRA-DELTA-07): campaign bits of one shared operator identifier as the campaign and the unrelated
background grow, plus controls. Synthetic genes only (tests.adversarial.helpers.gene); `pipeline.score_and_cluster`.

usage: uv run python scripts/score_growth_grid.py [--json out.json] [--md out.md]
The operator-unit mode is read from HYDRA_OPERATOR_UNITS (unset = default hard cut-off, `smooth` = graded).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hydra.pipeline import score_and_cluster  # noqa: E402
from hydra.synapse import Thresholds, build_genome  # noqa: E402
from tests.adversarial.helpers import gene  # noqa: E402

TRACKER = ("tracker_id", "synthetic-op-c1", "INF:operator:tracker")


def _bg(rows: dict, n: int) -> dict:
    for k in range(n):
        rows[f"bg{k}"] = [gene(f"bg{k}", "host", f"bg{k}.invalid", "INF:operator:host")]
    return rows


def _run(rows: dict):
    genomes = {i: build_genome(i, gs, {}) for i, gs in rows.items()}
    return score_and_cluster(genomes, Thresholds())


def _pair(pairs, a, b):
    pe = pairs.get((a, b)) or pairs.get((b, a))
    if pe is None:
        return 0.0, "not_scored"
    return round(float(pe.campaign_bits), 3), str(pe.score.hypothesis)


def probe(members: int, background: int, extra: tuple = ()) -> dict:
    """ASTRA probe corpus: `members` incidents share one tracker (and `extra` identifiers), unique hosts."""
    rows = {f"m{k}": [gene(f"m{k}", *TRACKER), gene(f"m{k}", "host", f"m{k}.invalid", "INF:operator:host")]
            + [gene(f"m{k}", *e) for e in extra] for k in range(members)}
    _r, pairs, cl, _ = _run(_bg(rows, background))
    bits, hyp = _pair(pairs, "m0", "m1")
    return {"members": members, "background": background, "total": members + background,
            "campaigns_among_members": len({cl.membership[f"m{k}"] for k in range(members)}),
            "pair_bits": bits, "pair_hypothesis": hyp}


def vendor(operators: int, per_op: int, background: int) -> dict:
    """Shared vendor tracker across unrelated operators: each operator's members share its own phone; every
    member carries the same vendor tracker. Expected: operators stay separate campaigns."""
    rows = {}
    for o in range(operators):
        for k in range(per_op):
            iid = f"o{o}m{k}"
            rows[iid] = [gene(iid, "tracker_id", "synthetic-vendor-sdk", "INF:operator:tracker"),
                         gene(iid, "entity_phone", f"+4850000{o:04d}", "INF:operator:phone"),
                         gene(iid, "host", f"{iid}.invalid", "INF:operator:host")]
    _r, pairs, cl, _ = _run(_bg(rows, background))
    camps = {cl.membership[i] for i in rows if not i.startswith("bg")}
    cross = [_pair(pairs, f"o{a}m0", f"o{b}m0") for a in range(operators) for b in range(a + 1, operators)]
    within = _pair(pairs, "o0m0", "o0m1")
    return {"operators": operators, "per_op": per_op, "background": background,
            "campaigns": len(camps), "within_bits": within[0], "within_hypothesis": within[1],
            "cross_max_bits": max(c[0] for c in cross),
            "cross_same_campaign": sum(c[1] == "same_campaign" for c in cross),
            "non_linking": len(camps) == operators and all(c[1] != "same_campaign" for c in cross)}


def chain(members: int, background: int) -> dict:
    """Chain of identifiers: m_k and m_{k+1} share phone_k (each phone held by two); no identifier covers all."""
    rows = {f"m{k}": [gene(f"m{k}", "host", f"m{k}.invalid", "INF:operator:host")] for k in range(members)}
    for k in range(members - 1):
        for iid in (f"m{k}", f"m{k + 1}"):
            rows[iid].append(gene(iid, "entity_phone", f"+4860000{k:04d}", "INF:operator:phone"))
    _r, pairs, cl, _ = _run(_bg(rows, background))
    bits, hyp = _pair(pairs, "m0", "m1")
    return {"members": members, "background": background,
            "campaigns_among_members": len({cl.membership[f"m{k}"] for k in range(members)}),
            "pair_bits": bits, "pair_hypothesis": hyp}


def cliff_stats(grid: list[dict]) -> dict:
    """Largest bit change between adjacent cells (one more member or one more background incident) and the number
    of adjacent steps that go the wrong way (bits rise with members or fall with background)."""
    cell = {(r["members"], r["background"]): r["pair_bits"] for r in grid}
    jumps, wrong = [], 0
    for (m, b), v in cell.items():
        if (m + 1, b) in cell:
            d = cell[(m + 1, b)] - v
            jumps.append(abs(d))
            wrong += d > 1e-9
        if (m, b + 1) in cell:
            d = cell[(m, b + 1)] - v
            jumps.append(abs(d))
            wrong += d < -1e-9
    return {"max_adjacent_jump_bits": round(max(jumps), 3), "steps_over_4_bits": sum(j > 4 for j in jumps),
            "wrong_direction_steps": wrong}


def render(paths: list[Path]) -> str:
    """Markdown heatmaps (pair m0-m1 campaign bits, hypothesis letter) for each JSON produced by this script."""
    out = []
    for p in paths:
        d = json.loads(p.read_text())
        g = d["grid"]
        ms = sorted({r["members"] for r in g})
        bs = sorted({r["background"] for r in g})
        cell = {(r["members"], r["background"]): r for r in g}
        out.append(f"### Mode `{d['mode']}`\n")
        out.append(f"Summary: `{json.dumps(d['summary'], sort_keys=True)}`; cliff: `{json.dumps(cliff_stats(g))}`\n")
        out.append("Cell = pair m0-m1 campaign bits (rounded) + hypothesis (S same_campaign, A abstain, "
                   "I independent); `*` = members split into more than one campaign.\n")
        out.append("| members \\ bg | " + " | ".join(str(b) for b in bs) + " |")
        out.append("|---" * (len(bs) + 1) + "|")
        for m in ms:
            row = []
            for b in bs:
                r = cell[(m, b)]
                h = {"same_campaign": "S", "abstain": "A", "independent": "I"}.get(r["pair_hypothesis"], "?")
                row.append(f"{r['pair_bits']:.0f}{h}{'*' if r['campaigns_among_members'] > 1 else ''}")
            out.append(f"| {m} | " + " | ".join(row) + " |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--render", type=Path, nargs="+", help="print markdown heatmaps of earlier --json outputs")
    ap.add_argument("--members", default="2-15")
    ap.add_argument("--background", default="0-40")
    a = ap.parse_args()
    if a.render:
        print(render(a.render))
        return
    lo, hi = map(int, a.members.split("-"))
    blo, bhi = map(int, a.background.split("-"))
    out: dict = {"mode": os.environ.get("HYDRA_OPERATOR_UNITS", "default") or "default"}
    out["grid"] = [probe(m, b) for m in range(lo, hi + 1) for b in range(blo, bhi + 1)]
    out["astra_rows"] = [probe(5, 15), probe(6, 15), probe(6, 20)]
    out["growing_two_ids"] = [probe(m, 20, (("entity_phone", "+48500999000", "INF:operator:phone"),))
                              for m in range(2, 16)]
    out["chain"] = [chain(m, b) for m in (3, 6, 10, 15) for b in (0, 20, 40)]
    out["vendor"] = [vendor(o, p, b) for o in (3, 4, 6) for p in (2, 3) for b in (0, 20, 40)]
    out["background_growth"] = [probe(5, b) for b in range(0, 101, 5)] + [probe(10, b) for b in range(0, 101, 5)]
    g = out["grid"]
    out["summary"] = {
        "cells": len(g),
        "split_cells": sum(r["campaigns_among_members"] > 1 for r in g),
        "same_campaign_cells": sum(r["pair_hypothesis"] == "same_campaign" for r in g),
        "vendor_non_linking": sum(v["non_linking"] for v in out["vendor"]), "vendor_cases": len(out["vendor"]),
        "chain_one_campaign": sum(c["campaigns_among_members"] == 1 for c in out["chain"]),
        "chain_cases": len(out["chain"]),
        "growing_two_ids_one_campaign": sum(r["campaigns_among_members"] == 1 for r in out["growing_two_ids"]),
    }
    text = json.dumps(out, indent=1, sort_keys=True)
    if a.json:
        a.json.write_text(text)
    print(json.dumps(out["summary"], sort_keys=True))


if __name__ == "__main__":
    main()
