"""Regenerate README.md §Results from a final run's outputs (results.json, errors.json, manifest.json, hidden_sim
score) so the README never cites a number that is not in the published artefacts.

    python -m eval.readme_results --dir <eval-sub/tag> [--readme README.md]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _ci(x) -> str:
    return f" [{x[0]:.2f}, {x[1]:.2f}]" if isinstance(x, list) and len(x) == 2 else ""


def _f(x, nd=3) -> str:
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def section(d: Path) -> str:
    r = json.loads((d / "results.json").read_text())
    man = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {}
    hs_p = d / "hidden_sim" / "full_score.json"
    hs = json.loads(hs_p.read_text()) if hs_p.exists() else {}
    ex_p = d / "extra_summary.json"
    ex = json.loads(ex_p.read_text()) if ex_p.exists() else {}
    m = r["meta"]
    prod = (m.get("commit") or {}).get("product") or man.get("product_sha")
    sysr = r["system"]

    def g(c, lvl, k):
        return sysr[c]["grouping"][lvl]["pairwise"][k]

    def loo(c):
        s = sysr[c]["loo"]["summary"]
        return f"{_f(s['accuracy'])}{_ci(s.get('accuracy_ci95'))} (n = {s['n']})"

    def rej(c):
        o = sysr[c]["open_set"]["summary"]
        comp = o.get("composition") or {}
        tail = (f": {comp['members_of_multi_member']} members of {comp['multi_member_campaigns']} multi-member "
                f"campaigns + {comp['singleton_campaigns']} singletons") if comp else ""
        return f"{_f(o['rejection_rate'])}{_ci(o.get('rejection_ci95'))} (n = {o['n']}{tail})"

    tr = (r.get("temporal_replay_all") or {}).get("sample", {}).get("summary") or {}
    tr_known = (f"; known-campaign items {tr.get('correct_assign')} of {tr.get('known_n')} correct, "
                f"{tr.get('false_assign')} false assign") if tr else ""
    hn = {c: sysr[c]["hard_negatives"] for c in sysr}
    L = ["## Results", "",
         f"From `eval/results.md` and `eval/errors.md`: run `{m.get('run_id')}` on product commit "
         f"`{(prod or '')[:12]}` "
         f"(harness `{(man.get('harness_sha') or '')[:12]}`, status **{man.get('status', 'n/a')}**, crashes "
         f"{r['crashes']['count']}). Identity, inputs and every command with its exit code are in "
         "`eval/published/manifest.json`; the protocol is in [docs/METHODOLOGY_EVAL.md](docs/METHODOLOGY_EVAL.md).",
         "- REAL = the 10 organiser incidents. Their numbers are in-sample: the thresholds were set while studying "
         "these incidents, and the labels are the team's hypothesis.",
         "- SYNTHETIC = generated variants of the same incidents plus public distractors.",
         "- TEMPORAL = REAL incidents in time order, with past-only rarity.",
         "- [a, b] = Wilson 95% interval, item unit; variants of one parent are dependent, see the campaign counts.",
         "- Limits: the sample-derived lexicon overlay and logo references are active here but absent in the jury "
         "container. In sample+syn leave-one-out the parent and sibling variants stay in the build. Labels have no "
         "second reviewer. See METHODOLOGY_EVAL §4e.", "",
         "| Check (NASK §6) | REAL sample (10) | Sample + 45 SYNTHETIC |", "|---|---|---|",
         f"| Campaign grouping: pairwise F1 / false-merge pairs | {_f(g('sample','campaign','f1'))} / "
         f"{g('sample','campaign','fp')} of {g('sample','campaign','negative_pairs')} | "
         f"{_f(g('sample+syn','campaign','f1'))} / {g('sample+syn','campaign','fp')} of "
         f"{g('sample+syn','campaign','negative_pairs')} |",
         f"| Campaign groups: pairwise F1 / false group merges | {_f(g('sample','group','f1'))} / "
         f"{g('sample','group','fp')} | {_f(g('sample+syn','group','f1'))} / {g('sample+syn','group','fp')} |",
         f"| New material, leave-one-incident-out assignment | {loo('sample')} | {loo('sample+syn')} |",
         f"| Held-out campaigns rejected as new pattern | {rej('sample')} | {rej('sample+syn')} |",
         f"| TEMPORAL past-only replay, assignment accuracy | {_f(tr.get('accuracy'))}{_ci(tr.get('accuracy_ci95'))}"
         f" (n = {tr.get('n')}{tr_known}) | n/a |",
         f"| Hard negatives joined: campaign / group | {hn['sample']['violations_campaign']} / "
         f"{hn['sample']['violations_group']} of {hn['sample']['pairs']} | "
         f"{hn['sample+syn']['violations_campaign']} / {hn['sample+syn']['violations_group']} of "
         f"{hn['sample+syn']['pairs']} |"]
    agg = (hs.get("summary") or {}).get("aggregate") or {}
    if agg:
        L.append(f"| hidden_sim (SYNTHETIC): assignment accuracy / missed known / abstain / foreign rejected | n/a | "
                 f"{_f(agg.get('assign_accuracy'))} / {_f(agg.get('pos_missed_new_rate'))} / "
                 f"{_f(agg.get('pos_abstain_rate'))} / {_f(agg.get('openset_rejection_strict'))} "
                 f"({agg.get('positives')} positives, {agg.get('negatives')} negatives) |")
    sr = r.get("selective_risk") or {}
    ft = sr.get("test_at_frozen") or {}
    if ft:
        ch = sr.get("chosen_on_calibration") or {}
        L.append(f"| Selective risk, test split at thresholds frozen on calibration (link {ch.get('link')}, abstain "
                 f"floor {ch.get('abstain_min')}): precision / recall known / foreign false-assign / abstention | "
                 f"n/a | "
                 f"{_f(ft.get('precision_automatic_assign'))}{_ci(ft.get('precision_ci95'))} / "
                 f"{_f(ft.get('recall_known'))} / {_f(ft.get('false_assign_rate_foreign'))} / "
                 f"{_f(ft.get('abstention_share'))} |")
    rt = (r.get("retrieval") or {}).get("filtered") or {}
    if rt:
        L.append(f"| Retrieval above 60 incidents ({rt['n_incidents']}, SYNTHETIC-heavy): candidate recall / campaign "
                 f"F1 / group F1 / false group merges | n/a | {_f(rt.get('candidate_recall'))} (of "
                 f"{rt.get('true_pairs')} pairs) / {_f(rt.get('campaign_f1'))} / {_f(rt.get('group_f1'))} / "
                 f"{rt.get('group_false_merges')} |")
    if ex:
        L.append(f"| Extra 15 organiser folders (development batch: inspected, motivated feature design, not used "
                 f"for tuning): decisions | {ex.get('decisions')} | n/a |")
    alt = (r.get("system_alt") or {}).get("alt_3_separate", {}).get("sample")
    L += ["", "Sensitivity and mechanism:"]
    if alt:
        L.append(f"- Alternative label (incident 3 as its own campaign): sample F1 "
                 f"{_f(alt['grouping']['campaign']['pairwise']['f1'])}, "
                 f"{alt['grouping']['campaign']['pairwise']['fp']} false merges, leave-one-out "
                 f"{_f(alt['loo']['summary']['accuracy'])}.")
    ma = r.get("mechanism_ablation") or {}
    if len(ma) > 1:
        L.append("- Removing one mechanism (sample+syn), campaign F1 / false group merges: " + "; ".join(
            f"{k} {_f(v.get('campaign_f1'))} / {v.get('group_false_merges')}" for k, v in ma.items()
            if isinstance(v, dict)) + ".")
    lad = r.get("ladder") or {}
    if lad:
        L.append("- Baselines on the same CORE features with the same tuning budget (sample+syn campaign F1): " +
                 ", ".join(f"{k} {_f(v.get('sample+syn', {}).get('campaign_pairwise', {}).get('f1'))}"
                           for k, v in lad.items()) + ".")
    L += ["", "Ten incidents show that the pipeline works end to end, not that it generalises; the hidden test set is "
          "the real measure.", ""]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--readme", type=Path, default=Path(__file__).resolve().parents[1] / "README.md")
    a = ap.parse_args()
    s = a.readme.read_text()
    i = s.index("## Results")
    j = s.index("\n## ", i + 5) + 1
    a.readme.write_text(s[:i] + section(a.dir) + s[j:])
    print(f"README §Results regenerated from {a.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
