"""Separate error report for the final evaluation (counts, denominators, 95% intervals, provenance label).

    python -m eval.error_report --results results.json [--hidden-sim rescore.json] [--extra extra_summary.json] \
        --md errors.md --json errors.json

Every row is labelled REAL in-sample (the 10 organiser incidents, thresholds set while studying them), SYNTHETIC
(generated from them, plus public distractors) or TEMPORAL REPLAY (REAL incidents in time order, past-only
rarity). Intervals: Wilson 95% for every count/denominator. Aggregates only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.metrics import wilson


def row(item: str, label: str, k: int | None, n: int | None, note: str = "") -> dict:
    rate = round(k / n, 4) if (k is not None and n) else None
    return {"item": item, "provenance": label, "count": k, "denominator": n, "rate": rate,
            "ci95": wilson(k, n) if (k is not None and n) else None, "note": note}


def build(res: dict, hs: dict | None, extra: dict | None) -> list[dict]:
    out: list[dict] = []
    lab = {"sample": "REAL in-sample", "sample+syn": "SYNTHETIC (10 REAL + 45 generated)"}
    for c, v in res.get("system", {}).items():
        g = v["grouping"]
        pc, pg = g["campaign"]["pairwise"], g["group"]["pairwise"]
        out.append(row("false campaign merges (pairs)", lab.get(c, c), pc["fp"], pc["negative_pairs"],
                       "pairs linked into one campaign whose labels differ"))
        out.append(row("false group merges (pairs)", lab.get(c, c), pg["fp"], pg["negative_pairs"],
                       "pairs in one campaign group whose group labels differ"))
        s = v["loo"]["summary"]
        out.append(row("abstention, leave-one-out", lab.get(c, c), s["abstain"], s["n"]))
        out.append(row("missed known campaign, leave-one-out (missed_new + wrong_campaign)", lab.get(c, c),
                       s["missed_new"] + s["wrong_campaign"], s["known_n"]))
        o = v["open_set"]["summary"]
        comp = o.get("composition") or {}
        note = (f"{comp.get('members_of_multi_member')} members of {comp.get('multi_member_campaigns')} multi-member "
                f"campaigns + {comp.get('singleton_campaigns')} singletons") if comp else ""
        k = round((o["rejection_rate"] or 0) * o["n"]) if o.get("rejection_rate") is not None else None
        out.append(row("new-campaign rejection, whole campaign held out", lab.get(c, c), k, o["n"], note))
    tr = res.get("temporal_replay_all", {}).get("sample") or res.get("temporal_replay", {})
    if tr and "summary" in tr:
        s = tr["summary"]
        out.append(row("assignment accuracy, past-only replay", "TEMPORAL REPLAY (REAL)",
                       s["correct_assign"] + s["correct_new"], s["n"]))
        out.append(row("new-pattern rejection, past-only replay", "TEMPORAL REPLAY (REAL)", s["correct_new"],
                       s["novel_n"]))
    rt = res.get("retrieval") or {}
    f, a = rt.get("filtered") or {}, rt.get("all_pairs") or {}
    if f:
        lost = f["true_pairs"] - round((f["candidate_recall"] or 0) * f["true_pairs"])
        out.append(row("retrieval: labelled same-campaign pairs lost by candidate filtering", "SYNTHETIC "
                       f"(bounded corpus, {f['n_incidents']} incidents)", lost, f["true_pairs"],
                       f"all-pairs oracle campaign F1 {a.get('campaign_f1')} vs product {f.get('campaign_f1')}"))
        cn = f.get("connectivity") or {}
        if cn:
            out.append(row("retrieval: campaigns split by candidate filtering", "SYNTHETIC (bounded corpus)",
                           cn["campaigns"] - cn["connected"], cn["campaigns"]))
    if hs:
        pv = (hs.get("summary") or hs).get("per_variant", {})
        agg = (hs.get("summary") or hs).get("aggregate", {})
        for vt, cnt in sorted(pv.items()):
            n = sum(cnt.values())
            ok = cnt.get("ok", 0) + cnt.get("ok_new", 0)
            neg = "ok_new" in cnt or any(k.startswith("false") for k in cnt)
            item = (f"hidden_sim negative `{vt}`: wrongly linked" if neg else f"hidden_sim variant `{vt}`: missed")
            out.append(row(item, "SYNTHETIC (hidden_sim seed 1)", n - ok, n,
                           ", ".join(f"{k} {v}" for k, v in sorted(cnt.items()))))
        if agg:
            p = agg.get("positives")
            out.append(row("hidden_sim abstention on positives", "SYNTHETIC (hidden_sim seed 1)",
                           round((agg.get("pos_abstain_rate") or 0) * p) if p else None, p))
    if extra:
        for k, v in sorted(extra.get("decisions", {}).items()):
            out.append(row(f"extra organiser batch (15 folders): decision `{k}`",
                           "REAL development batch (inspected; motivated feature design; not used for threshold "
                           "tuning), unlabelled", v, extra.get("n"),
                           "decision counts only; not a held-out test; no labels, so no accuracy"))
    return out


def to_md(rows: list[dict], header: str) -> str:
    L = [header, "", "| Item | Provenance | Count / denominator | Rate [95% CI] | Note |", "|---|---|---|---|---|"]
    for r in rows:
        ci = f" [{r['ci95'][0]:.2f}, {r['ci95'][1]:.2f}]" if r["ci95"] else ""
        rate = f"{r['rate']:.3f}{ci}" if r["rate"] is not None else "n/a"
        L.append(f"| {r['item']} | {r['provenance']} | {r['count']} / {r['denominator']} | {rate} | {r['note']} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--hidden-sim", type=Path, default=None)
    ap.add_argument("--extra", type=Path, default=None)
    ap.add_argument("--md", type=Path, required=True)
    ap.add_argument("--json", type=Path, required=True)
    ap.add_argument("--title", default="Error report")
    ap.add_argument("--steps", type=Path, default=None, help="steps.jsonl of submission_run.sh (stage status)")
    a = ap.parse_args()
    res = json.loads(a.results.read_text())
    hs = json.loads(a.hidden_sim.read_text()) if a.hidden_sim and a.hidden_sim.exists() else None
    ex = json.loads(a.extra.read_text()) if a.extra and a.extra.exists() else None
    rows = build(res, hs, ex)
    m = res.get("meta", {})
    head = (f"# {a.title}\n\nRun `{m.get('run_id')}`, product commit `{(m.get('commit') or {}).get('product')}`. "
            "Counts with denominators and Wilson 95% intervals (item unit; variants of one parent are dependent, so "
            "campaign counts are given in the notes). REAL in-sample numbers use thresholds set while "
            "studying the same incidents; SYNTHETIC material is derived from them.")
    if a.steps and a.steps.exists():
        from eval.manifest import stage_status

        st = [json.loads(x) for x in a.steps.read_text().splitlines() if x.strip()]
        bad = [x for x in st if x.get("exit_code") != 0]
        ss = stage_status(st + [{"name": "error_report", "exit_code": 0}], res.get("crashes", {}).get("count"))
        missing = [k for k, v in ss["stages"].items() if v == "missing"]
        if missing:
            head += f"\n\nMissing stages: {', '.join(missing)}."
        head += ("\n\nStages: " + ", ".join(f"{x['name']} exit {x['exit_code']}" for x in st) +
                 (f". **Status: {'FAIL' if any(x['name'] == 'eval' for x in bad) else 'PARTIAL'}** "
                  f"(failed: {', '.join(x['name'] for x in bad)})." if bad else ". Status: all stages exit 0."))
    labels_dir = Path.home() / "HydraNexus" / "data" / "nask" / "labels"
    if not any(labels_dir.glob("*adjudicated*")):
        head += ("\n\nLabels are the team's hypothesis: no independently adjudicated labels existed at run time.")
    a.md.write_text(to_md(rows, head), encoding="utf-8")
    a.json.write_text(json.dumps({"run_id": m.get("run_id"), "rows": rows}, indent=1))
    print(f"{len(rows)} rows -> {a.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
