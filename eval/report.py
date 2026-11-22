"""results.json → eval/results.md + PNG charts. Only called by eval/run.py (never edit results.md by hand)."""

from __future__ import annotations

import time
from pathlib import Path

# Reference categorical palette (dataviz skill, light mode); slots used in fixed order.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def _home(p: str) -> str:
    """Strip machine-specific home prefixes (laptop /Users/x, server /home/x)."""
    import re

    return re.sub(r"^(/Users|/home)/[^/]+", "~", str(p))


def _fmt(x) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.3f}"
    return str(x)


def _style(ax, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _bars(path: Path, title: str, cats: list[str], series: dict[str, list[float | None]], ylim=(0, 1.05)) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.5, 3.6), dpi=130, facecolor=SURFACE)
    n = len(series)
    w = 0.8 / max(1, n)
    for k, (name, vals) in enumerate(series.items()):
        xs = [i + (k - (n - 1) / 2) * w for i in range(len(cats))]
        ys = [v if v is not None else 0 for v in vals]
        bars = ax.bar(xs, ys, w * 0.9, color=SERIES[k % len(SERIES)], label=name, edgecolor=SURFACE, linewidth=1)
        for b, v in zip(bars, vals, strict=False):
            ax.text(b.get_x() + b.get_width() / 2, (v or 0) + 0.01, "n/a" if v is None else f"{v:.2f}",
                    ha="center", va="bottom", fontsize=6.5, color=INK2)
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels(cats, fontsize=8, color=INK2)
    if ylim:
        ax.set_ylim(*ylim)
    _style(ax, title)
    if n > 1:
        ax.legend(frameon=False, fontsize=8, labelcolor=INK2, ncol=min(4, n), loc="upper left",
                  bbox_to_anchor=(0, -0.12))
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def charts(res: dict, out: Path) -> list[str]:
    made = []
    lad = res.get("ladder", {})
    if lad:
        short = {"B0_phash": "B0 pHash", "B1_text": "B1 text", "B2_naive_sum": "B2 naive", "B3_rarity_bits":
                 "B3 rarity", "LR_fusion": "LR", "B3_CORE_rarity_bits_tiers_caps": "CORE",
                 "CORE_tuned_on_syn_dev": "CORE tuned"}
        for corpus in ("sample", "sample+syn"):
            names = [n for n in lad if corpus in lad[n]]
            series = {
                "campaign pairwise F1": [lad[n][corpus]["campaign_pairwise"]["f1"] for n in names],
                "campaign B-cubed F1": [lad[n][corpus]["campaign_bcubed"]["f1"] for n in names],
                "LOO assignment accuracy": [lad[n][corpus].get("loo_accuracy") for n in names],
                "open-set rejection": [lad[n][corpus].get("open_set_rejection") for n in names],
            }
            fn = f"chart_ladder_{corpus.replace('+', '_')}.png"
            label = "REAL" if corpus == "sample" else "SYNTHETIC mix"
            _bars(out / fn, f"Baseline ladder, same features — {corpus} ({label})", [short.get(n, n) for n in names],
                  series)
            made.append(fn)
    ab = res.get("ablation")
    if ab:
        fams = [k for k in ab if k.startswith("-")]
        _bars(out / "chart_ablation.png", "Per-family ablation (sample+syn): drop in metric when family removed",
              fams, {"Δ LOO accuracy": [ab[f]["delta_loo_accuracy"] for f in fams],
                     "Δ campaign pairwise F1": [ab[f]["delta_campaign_f1"] for f in fams]}, ylim=None)
        made.append("chart_ablation.png")
    rob = {k: v for k, v in res.get("robustness", {}).items() if isinstance(v, dict) and "accuracy" in v}
    if rob:
        cases = list(rob)
        _bars(out / "chart_robustness.png", "Incomplete data and formats (sample+syn, SYNTHETIC variants)", cases,
              {"assignment accuracy": [rob[c]["accuracy"] for c in cases],
               "agreement with undegraded": [rob[c]["agreement_with_undegraded"] for c in cases],
               "gene stability": [rob[c]["mean_gene_stability"] for c in cases]})
        made.append("chart_robustness.png")
    sw = res.get("core_sweep")
    if isinstance(sw, dict) and sw.get("grid"):
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        corpora = sorted({r["corpus"] for r in sw["grid"]})
        fig, axes = plt.subplots(1, len(corpora), figsize=(5 * len(corpora), 3.4), dpi=130, facecolor=SURFACE,
                                 squeeze=False)
        kits = sorted({r["kit"] for r in sw["grid"]})
        for ax, corpus in zip(axes[0], corpora, strict=False):
            for k, kit in enumerate(kits):
                pts = sorted((r["link"], r["campaign_f1"]) for r in sw["grid"] if r["corpus"] == corpus
                             and r["kit"] == kit)
                ax.plot([p[0] for p in pts], [p[1] for p in pts], color=SERIES[k % len(SERIES)], linewidth=2,
                        marker="o", markersize=4, label=f"kit {kit:g}")
            ax.axvline(sw["defaults"]["link"], color=INK2, linewidth=0.8, linestyle=":")
            ax.set_ylim(0, 1.05)
            ax.set_xlabel("link threshold (bits)", fontsize=8, color=INK2)
            _style(ax, f"CORE campaign pairwise F1 — {corpus}")
        axes[0][0].legend(frameon=False, fontsize=7, labelcolor=INK2)
        fig.tight_layout()
        fig.savefig(out / "chart_core_sweep.png", facecolor=SURFACE)
        plt.close(fig)
        made.append("chart_core_sweep.png")
    cov = res.get("coverage", {}).get("sample")
    if cov:
        fams = list(cov["family"])
        _bars(out / "chart_coverage.png", "Feature coverage: share of SAMPLE incidents with an observed gene",
              fams, {"family observed": [cov["family"][f] for f in fams]})
        made.append("chart_coverage.png")
    return made


def _comp(o: dict) -> str:
    c = o.get("composition") or {}
    if not c:
        return ""
    return (f": {c['members_of_multi_member']} members of {c['multi_member_campaigns']} multi-member campaigns + "
            f"{c['singleton_campaigns']} singletons")


def _ci(x) -> str:
    return f" [{x[0]:.2f}, {x[1]:.2f}]" if isinstance(x, list) and len(x) == 2 else ""


def _grouping_rows(c: str, v: dict) -> list[str]:
    out = []
    for lvl in ("campaign", "group"):
        g = v["grouping"][lvl]
        p = g["pairwise"]
        bs = g.get("bootstrap") or {}
        out.append(f"| {c} | {lvl} | {_fmt(p['precision'])} | {_fmt(p['recall'])} | {_fmt(p['f1'])}"
                   f"{_ci(bs.get('pairwise_f1_ci95'))} | {_fmt(g['bcubed']['f1'])}{_ci(bs.get('bcubed_f1_ci95'))} | "
                   f"{p['fp']} of {p['negative_pairs']} | {g['pred_clusters']}/{g['true_clusters']} |")
    pc = v["grouping"]["campaign"].get("pairwise_confirmed_only")
    if pc:
        d = v["grouping"]["campaign"]["disputed_pairs"]
        out.append(f"| {c} | campaign, confirmed pairs only ({d['n']} disputed left out, {d['linked_by_system']} of "
                   f"them linked) | {_fmt(pc['precision'])} | {_fmt(pc['recall'])} | {_fmt(pc['f1'])} | – | "
                   f"{pc['fp']} of {pc['negative_pairs']} | – |")
    return out


GROUP_HDR = ["| Corpus | Level | Pairwise P | Pairwise R | Pairwise F1 | B-cubed F1 | False-merge pairs | "
             "Pred/true clusters |", "|---|---|---|---|---|---|---|---|"]
ASSIGN_HDR = ["| Corpus | LOO n | LOO accuracy | known-campaign acc. | novel rejection (LOO) | abstain | "
              "group-level LOO acc. | held-out-campaign rejection | held-out: sent to a sibling campaign of the "
              "same group | held-out: group level correct | hard-negative violations (campaign / group) |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]


def _assign_row(c: str, v: dict) -> str:
    s, o, h = v["loo"]["summary"], v["open_set"]["summary"], v["hard_negatives"]
    gl = s.get("group_level") or {}
    return (f"| {c} | {s['n']} | {_fmt(s['accuracy'])}{_ci(s.get('accuracy_ci95'))} | "
            f"{_fmt(s['known_accuracy'])}{_ci(s.get('known_accuracy_ci95'))} (n={s['known_n']}) | "
            f"{_fmt(s['novel_rejection'])}{_ci(s.get('novel_rejection_ci95'))} (n={s['novel_n']}) | "
            f"{_fmt(s['abstain_rate'])} | {_fmt(gl.get('accuracy'))} | "
            f"{_fmt(o['rejection_rate'])}{_ci(o.get('rejection_ci95'))} (n={o['n']}{_comp(o)}) | "
            f"{o.get('false_assign_to_sibling_in_same_group', 'n/a')} | {_fmt(o.get('group_level_correct'))} | "
            f"{h['violations_campaign']} of {h['pairs']} / {h['violations_group']} |")


def _alt_rows(res: dict, corpus: str) -> list[str]:
    L = []
    for alt, by in res.get("system_alt", {}).items():
        v = by.get(corpus)
        if not v:
            continue
        g = v["grouping"]
        o = v["open_set"]["summary"]
        L.append(f"| `{alt}` ({by.get('note')}) | {_fmt(g['campaign']['pairwise']['f1'])} | "
                 f"{_fmt(g['campaign']['bcubed']['f1'])} | {g['campaign']['pairwise']['fp']} | "
                 f"{_fmt(g['group']['pairwise']['f1'])} | {_fmt(v['loo']['summary']['accuracy'])} | "
                 f"{_fmt(o['rejection_rate'])} | {o.get('false_assign_to_sibling_in_same_group', 'n/a')} |")
    return L


def _ladder_rows(res: dict, corpus: str) -> list[str]:
    L = ["| Scorer | Features | Threshold (source) | Campaign pairwise F1 | B-cubed F1 | False merges | Group F1 | "
         "LOO acc. | Held-out rejection |", "|---|---|---|---|---|---|---|---|---|"]
    for n, by in res.get("ladder", {}).items():
        r = by.get(corpus)
        if not r:
            continue
        fb = " (LR fell back to B3)" if r.get("lr_fallback") else ""
        L.append(f"| {n}{fb} | {r.get('genes', 'stub')} | {_fmt(r['threshold'])} "
                 f"({r.get('threshold_source', 'syn-dev')}) | {_fmt(r['campaign_pairwise']['f1'])} | "
                 f"{_fmt(r['campaign_bcubed']['f1'])} | {r['campaign_pairwise']['fp']} | "
                 f"{_fmt(r['group_pairwise']['f1'])} | {_fmt(r.get('loo_accuracy'))} | "
                 f"{_fmt(r.get('open_set_rejection'))} |")
    return L


def write(res: dict, out: Path) -> Path:
    made = charts(res, out)
    m = res["meta"]
    L: list[str] = []
    L += ["# Evaluation results", "",
          f"Generated by `eval/run.py` on {time.strftime('%Y-%m-%d %H:%M %Z')} (run `{m.get('run_id')}`). "
          "Do not edit by hand.", "",
          f"- Backend: **{m['backend']}**; evaluated commit: "
          f"`{(m.get('commit') or {}).get('product') or (m.get('commit') or {}).get('checkout') or 'unknown'}`"
          f"{' (hydra/ has uncommitted changes)' if (m.get('commit') or {}).get('hydra_dirty') else ''}; "
          f"eval state (isolated): `{_home(m.get('state_dir', 'n/a'))}`.",
          f"- Labels: `{_home(m['labels'])}` (decided by: {m.get('labels_decided_by')})"
          " — development hypothesis, not organiser truth. Label status per incident (campaign / group): "
          + ", ".join(f"{i} {v['label']}/{v['group']}" for i, v in (m.get("label_status") or {}).items()) + ".",
          "- Disputed pairs (relation differs between the adopted and the alternative label set) are never "
          "automatic negatives: every table also gives the result under the alternative set, and the "
          "'confirmed pairs only' row leaves them out.",
          f"- Crashes: **{res['crashes']['count']}**.",
          "- Intervals: [a, b] after a number is a 95% interval — Wilson for rates with a count, campaign-level "
          "bootstrap (1,000 draws, seed 0) for pairwise and B-cubed F1. With 10 incidents they are wide.",
          "- **Leakage statement (G10).** CORE's default thresholds (link 10, kit 6) were set by the team while "
          "studying the same 10 sample incidents, so every `sample` number at the defaults is IN-SAMPLE. "
          "`sample+syn` variants are derived from the sample, so they are not independent test data either. "
          "The row `CORE_tuned_on_syn_dev` reports the sample at a link threshold chosen on `syn-dev` alone. "
          "Labels are the team's development hypothesis. No number here estimates hidden-test performance.",
          "- Sections: **A. REAL** (10 organiser incidents only), **B. SYNTHETIC** (corpora containing generated "
          "material; "
          f"{m['synthetic_note']}), **C. TEMPORAL REPLAY** (REAL incidents in time order). Never mix them in a "
          "headline.", ""]
    sysr = res["system"]
    # ------------------------------------------------------------------ A. REAL
    L += ["## A. REAL: 10 organiser incidents (`sample`)", "", "### A1. Grouping", ""] + GROUP_HDR + \
        _grouping_rows("sample", sysr["sample"])
    L += ["", "### A2. Assignment of new material and open set", ""] + ASSIGN_HDR + [_assign_row("sample",
                                                                                         sysr["sample"])]
    s = sysr["sample"]["loo"]["summary"]
    L += ["", "LOO outcomes: " + ", ".join(f"{k} {s[k]}" for k in ("correct_assign", "wrong_campaign", "missed_new",
                                                                  "correct_new", "false_assign", "abstain")),
          "", "Predicted multi-member campaigns: "
          + ("; ".join("{" + ", ".join(x) + "}" for x in sysr["sample"]["predicted_campaigns"]) or "none")]
    hn = sysr["sample"]["hard_negatives"]["rows"]
    if hn:
        L += ["", "Hard negatives (must not be one campaign):", "",
              "| a | b | score (bits) | same campaign | same group |", "|---|---|---|---|---|"]
        L += [f"| {r['a']} | {r['b']} | {_fmt(r['score'])} | {r['same_campaign']} | {r['same_group']} |" for r in hn]
    L += ["", "### A3. Label sensitivity (both label sets, always)", "",
          "| Label set | Campaign F1 | B-cubed F1 | False merges | Group F1 | LOO acc. | Held-out rejection | "
          "Sent to sibling |", "|---|---|---|---|---|---|---|---|",
          f"| adopted | {_fmt(sysr['sample']['grouping']['campaign']['pairwise']['f1'])} | "
          f"{_fmt(sysr['sample']['grouping']['campaign']['bcubed']['f1'])} | "
          f"{sysr['sample']['grouping']['campaign']['pairwise']['fp']} | "
          f"{_fmt(sysr['sample']['grouping']['group']['pairwise']['f1'])} | "
          f"{_fmt(sysr['sample']['loo']['summary']['accuracy'])} | "
          f"{_fmt(sysr['sample']['open_set']['summary']['rejection_rate'])} | "
          f"{sysr['sample']['open_set']['summary'].get('false_assign_to_sibling_in_same_group')} |"] + \
        _alt_rows(res, "sample")
    L += ["", "### A4. Baseline ladder on REAL (same features as the system, same tuning budget)", "",
          "Every method gets one threshold, chosen on `syn-dev` from its grid by max campaign pairwise F1 "
          "(ties → higher). `CORE_tuned_on_syn_dev` applies the same rule to CORE's link threshold.", ""] + \
        _ladder_rows(res, "sample")
    # ------------------------------------------------------------------ B. SYNTHETIC
    L += ["", "## B. SYNTHETIC: corpora with generated material", "",
          "Corpora: " + "; ".join(f"`{k}` = {v}" for k, v in m["corpora"].items()) + ".", "",
          "### B1. Grouping and assignment on `sample+syn`", ""] + GROUP_HDR + _grouping_rows("sample+syn",
                                                                                          sysr["sample+syn"])
    L += [""] + ASSIGN_HDR + [_assign_row("sample+syn", sysr["sample+syn"])]
    L += ["", "| Label set | Campaign F1 | B-cubed F1 | False merges | Group F1 | LOO acc. | Held-out rejection | "
          "Sent to sibling |", "|---|---|---|---|---|---|---|---|"] + _alt_rows(res, "sample+syn")
    L += ["", "### B2. Baseline ladder on `sample+syn`", ""] + _ladder_rows(res, "sample+syn")
    L += ["", "### B3. Robustness (variants of every sample incident)", "",
          "Duplicates are added to the full corpus; every other variant is assigned into a build without its "
          "parent and compared with the parent's own leave-one-out outcome.", "",
          "| Case | n | Linked to parent | Campaign inflation | Accuracy | Agreement with undegraded | "
          "Gene stability | Crashes |", "|---|---|---|---|---|---|---|---|"]
    for k, v in res.get("robustness", {}).items():
        if not isinstance(v, dict):
            L.append(f"| {k} | {v} | | | | | | |")
            continue
        L.append(f"| {k} | {v['n']} | {_fmt(v.get('linked_to_parent_rate'))} | {_fmt(v.get('campaign_inflation'))} | "
                 f"{_fmt(v.get('accuracy'))} | {_fmt(v.get('agreement_with_undegraded'))} | "
                 f"{_fmt(v.get('mean_gene_stability'))} | {v.get('crashes', 0)} |")
    ab = res.get("ablation")
    if ab:
        L += ["", "### B4. Per-family ablation (`sample+syn`)", "",
              "| Variant | LOO accuracy | Δ LOO | Campaign F1 | Δ F1 | Group F1 | False merges |",
              "|---|---|---|---|---|---|---|"]
        for k, v in ab.items():
            if k == "corpus" or k.startswith("only-"):
                continue
            L.append(f"| {k} | {_fmt(v['loo_accuracy'])} | {_fmt(v.get('delta_loo_accuracy'))} | "
                     f"{_fmt(v['campaign_f1'])} | {_fmt(v.get('delta_campaign_f1'))} | {_fmt(v['group_f1'])} | "
                     f"{v['false_merges']} |")
        only = {k: v for k, v in ab.items() if k.startswith("only-")}
        if only:
            L += ["", "Single family (all other families dropped):", "",
                  "| Only | LOO accuracy | Campaign F1 | Group F1 | False merges |", "|---|---|---|---|---|"]
            for k, v in only.items():
                L.append(f"| {k[5:]} | {_fmt(v['loo_accuracy'])} | {_fmt(v['campaign_f1'])} | "
                         f"{_fmt(v['group_f1'])} | {v['false_merges']} |")
    sw = res.get("core_sweep")
    if isinstance(sw, dict) and sw.get("grid"):
        d = sw["defaults"]
        L += ["", "### B5. CORE sensitivity (thresholds and caps)", "",
              f"CORE defaults: link {d['link']}, kit {d['kit']}, FAMILY_CAP {d['FAMILY_CAP']}, "
              f"PLATFORM_KIT_CAP {d['PLATFORM_KIT_CAP']}. Main results use the defaults; the sweep is reported.", ""]
        for c, v in (sw.get("consistency") or {}).items():
            L.append(f"Consistency `{c}`: default point F1 {v['sweep_f1']} / false merges {v['sweep_fm']} vs main "
                     f"result {v['system_f1']} / {v['system_fm']}: {'OK' if v['ok'] else 'MISMATCH'}.")
        pl = sw.get("plateau") or {}
        if pl:
            L += ["", f"Most stable point on `{pl['corpus']}` (mean (campaign F1 + group F1)/2 − 0.01·false merges "
                  f"over the point and its grid neighbours): link {pl['link']}, kit {pl['kit']}."]
        L += ["", "| Corpus | link | kit | Campaign F1 | B-cubed F1 | False merges | Group F1 | Group false merges |",
              "|---|---|---|---|---|---|---|---|"]
        for r in sw["grid"]:
            L.append(f"| {r['corpus']} | {r['link']:g} | {r['kit']:g} | {_fmt(r['campaign_f1'])} | "
                     f"{_fmt(r['campaign_bcubed_f1'])} | {r['false_merges']} | {_fmt(r['group_f1'])} | "
                     f"{r['group_false_merges']} |")
        if sw.get("link_axis"):
            L += ["", "| Corpus | link | LOO acc. | known acc. | novel rejection | held-out rejection |",
                  "|---|---|---|---|---|---|"]
            for r in sw["link_axis"]:
                L.append(f"| {r['corpus']} | {r['link']:g} | {_fmt(r['loo_accuracy'])} | {_fmt(r['known_accuracy'])}"
                         f" | {_fmt(r['novel_rejection'])} | {_fmt(r['open_set_rejection'])} |")
        if sw.get("caps"):
            L += ["", "| Corpus | FAMILY_CAP | PLATFORM_KIT_CAP | Campaign F1 | False merges | Group F1 |",
                  "|---|---|---|---|---|---|"]
            for r in sw["caps"]:
                L.append(f"| {r['corpus']} | {r['FAMILY_CAP']:g} | {r['PLATFORM_KIT_CAP']:g} | "
                         f"{_fmt(r['campaign_f1'])} | {r['false_merges']} | {_fmt(r['group_f1'])} |")
    rt = res.get("retrieval")
    if rt:
        comp = ", ".join(f"{k} {v}" for k, v in rt["composition"].items())
        L += ["", "### B6. Retrieval above CORE's all-pairs cut-off (60 incidents)", "",
              f"Corpus: {sum(rt['composition'].values())} incidents ({comp}). {rt['note']}.", "",
              "| Mode | Candidate pairs / all pairs | Candidate recall (labelled same-campaign pairs) | Campaign F1 | "
              "False merges | Group F1 | Build wall s | Peak memory MB (tracemalloc heap or RSS increase) |",
              "|---|---|---|---|---|---|---|---|"]
        for mode in ("filtered", "all_pairs"):
            r = rt.get(mode) or {}
            if r:
                cn = r.get("connectivity") or {}
                L.append(f"| {mode} | {r['candidate_pairs']} / {r['all_pairs']} | {_fmt(r['candidate_recall'])} "
                         f"(of {r['true_pairs']}); campaigns connected {cn.get('connected')}/{cn.get('campaigns')} | "
                         f"{_fmt(r['campaign_f1'])} | {r['false_merges']} | "
                         f"{_fmt(r['group_f1'])} | {r['build_wall_s']} | "
                         f"{r.get('peak_rss_increase_mb', r.get('peak_python_heap_mb'))} |")
    # ------------------------------------------------------------------ C. TEMPORAL REPLAY
    tr = res.get("temporal_replay")
    L += ["", "## C. TEMPORAL REPLAY: REAL incidents in time order", ""]
    if tr:
        s = tr["summary"]
        L += ["Each sample incident, in order of `observed_at`, is assigned against a build of the incidents seen "
              "before it (the first one starts the corpus).", "",
              "Order: " + ", ".join(f"{i} ({(d or '?')[:10]})" for i, d in tr["order"]), "",
              f"Accuracy {_fmt(s['accuracy'])} (n={s['n']}): known-campaign {_fmt(s['known_accuracy'])} "
              f"(n={s['known_n']}), new-pattern rejection {_fmt(s['novel_rejection'])} (n={s['novel_n']}), "
              f"group level {_fmt((s.get('group_level') or {}).get('accuracy'))}.", "",
              "| # | Incident | observed_at | Decision | Outcome | Group outcome |", "|---|---|---|---|---|---|"]
        for k, r in enumerate(tr["rows"], 1):
            L.append(f"| {k} | {r['incident']} | {(r.get('observed_at') or '?')[:19]} | {r['decision']} | "
                     f"{r['outcome']} | {r.get('group_outcome', 'n/a')} |")
    else:
        L.append("Not run (stub backend).")
    # ------------------------------------------------------------------ stability / fidelity / past-only
    st, fi, ta = res.get("stability") or {}, res.get("fidelity") or {}, res.get("temporal_replay_all") or {}
    if st or fi or ta:
        L += ["", "## C2. Stability, explanation fidelity and past-only replay (both corpora; `sample` = REAL, "
              "`sample+syn` = SYNTHETIC mix)", ""]
        if st:
            L += ["Partition stability (G08): link ±2, kit ±2 and leave-one-family-out re-clustering; a campaign "
                  "is stable in a perturbation when its best-matching cluster has Jaccard ≥ 0.8.", "",
                  "| Corpus | Perturbations | Campaigns | Mean stability | Campaigns < 0.5 | AMI min / mean |",
                  "|---|---|---|---|---|---|"]
            for c, v in st.items():
                for part in ("thresholds_only", "leave_one_family_out", "all"):
                    x = (v or {}).get(part) or {}
                    if x:
                        L.append(f"| {c} ({part}) | {x['perturbations']} | {x['campaigns']} | "
                                 f"{_fmt(x['mean_stability'])} | {x['unstable_campaigns_lt_0_5']} | "
                                 f"{_fmt(x['ami_min'])} / {_fmt(x['ami_mean'])} |")
        if fi:
            L += ["", "Explanation fidelity (G11): delete the genes behind the top-k stated reasons of every linked "
                  "pair and rescore with the same rarity.", "",
                  "| Corpus | Linked pairs | Fidelity top-1 (link lost) | Fidelity top-3 | "
                  "Top-3 share of reason bits |",
                  "|---|---|---|---|---|"]
            for c, v in fi.items():
                if isinstance(v, dict) and "linked_pairs" in v:
                    L.append(f"| {c} | {v['linked_pairs']} | {_fmt(v['fidelity_top1'])} | "
                             f"{_fmt(v['fidelity_top3'])} | {_fmt(v['top3_bit_share_mean'])} |")
            L.append("")
            L.append("Low fidelity means the link survives the deletion: the stated top reasons are not the only "
                     "evidence (redundancy), which is robustness rather than an unfaithful explanation.")
        if ta:
            L += ["", "Past-only replay (G09) next to standard leave-one-out:", "",
                  "| Corpus | Dated | Months | Past-only accuracy | LOO accuracy | Difference |",
                  "|---|---|---|---|---|---|"]
            for c, v in ta.items():
                if "summary" in v:
                    s_ = v["summary"]
                    L.append(f"| {c} | {v['dated']} | {', '.join(v['months'])} | {_fmt(s_['accuracy'])}"
                             f"{_ci(s_.get('accuracy_ci95'))} | {_fmt(v['loo_accuracy'])} | "
                             f"{_fmt(v['past_only_minus_loo'])} |")
                else:
                    L.append(f"| {c} | {v.get('note')} | | | | |")
            L.append("")
            L.append("AUT-style per-month curve: not computed, the dated material spans fewer than 3 months.")
    # ------------------------------------------------------------------ method protocol
    sr, cs, ma = res.get("selective_risk"), res.get("conformal_stages"), res.get("mechanism_ablation")
    if sr or cs or ma:
        L += ["", "## C3. Method protocol: selective risk, conformal stages, mechanism ablations", ""]
    if isinstance(sr, dict) and sr.get("curve"):
        sp = sr.get("split", {})
        ch = sr.get("chosen_on_calibration") or {}
        L += [f"Selective risk (SYNTHETIC, campaign- and origin-disjoint split: calibration {sp.get('calib_n')} "
              f"incidents / {sp.get('calib_campaigns')} campaigns, test {sp.get('test_n')} / "
              f"{sp.get('test_campaigns')}). Thresholds chosen on calibration as max correct automatic assignments "
              f"with wrong-assignment rate ≤ {sr['wrong_limit']}, then frozen: link {ch.get('link')}, abstain floor "
              f"{ch.get('abstain_min')}.", "",
              "| Split | link | abstain floor | Coverage | Risk (auto) | Precision of assignments | Recall known | "
              "False-assign rate foreign | Abstention |", "|---|---|---|---|---|---|---|---|---|"]
        for split, pts in sr["curve"].items():
            for p_ in pts:
                mark = " **(frozen)**" if (split == "risk-test" and p_["link"] == ch.get("link")
                                            and p_["abstain_min"] == ch.get("abstain_min")) else ""
                L.append(f"| {split}{mark} | {p_['link']:g} | {p_['abstain_min']:g} | {_fmt(p_['coverage'])} | "
                         f"{_fmt(p_['risk_automatic'])} | {_fmt(p_['precision_automatic_assign'])}"
                         f"{_ci(p_.get('precision_ci95'))} | {_fmt(p_['recall_known'])}"
                         f"{_ci(p_.get('recall_known_ci95'))} | {_fmt(p_['false_assign_rate_foreign'])}"
                         f"{_ci(p_.get('false_assign_ci95'))} | {_fmt(p_['abstention_share'])} |")
    if cs:
        L += ["", "Conformal staged report (leave-one-out items; no nominal coverage claimed for the pipeline):", "",
              "| Corpus | Known n | Retrieval found true campaign | True campaign in conformal set | Final correct "
              "assign | Foreign n | Raw set empty | Final rejection | Decided by |",
              "|---|---|---|---|---|---|---|---|---|"]
        for c, v in cs.items():
            L.append(f"| {c} | {v['known_n']} | {v['retrieved_true_campaign']}{_ci(v['retrieved_ci95'])} | "
                     f"{v['true_campaign_in_conformal_set']}{_ci(v['in_set_ci95'])} | "
                     f"{v['final_correct_assign']}{_ci(v['final_ci95'])} | {v['foreign_n']} | {v['raw_set_empty']}"
                     f"{_ci(v['raw_set_rejection_ci95'])} | {v['final_rejection']}{_ci(v['final_rejection_ci95'])} | "
                     f"{v['decided_by']} |")
    if isinstance(ma, dict) and len(ma) > 1:
        L += ["", f"Mechanism ablations (corpus `{ma.get('corpus')}`, SYNTHETIC mix):", "",
              "| Mechanism removed | Campaign F1 | False campaign merges | Group F1 | False group merges | LOO acc. | "
              "Missed known / n | Abstain / n |", "|---|---|---|---|---|---|---|---|"]
        for k, v in ma.items():
            if k == "corpus" or not isinstance(v, dict):
                continue
            L.append(f"| {k} | {_fmt(v['campaign_f1'])} | {v['false_merges']} | {_fmt(v['group_f1'])} | "
                     f"{v['group_false_merges']} | {_fmt(v['loo_accuracy'])} | {v['missed_known']} / {v['known_n']} | "
                     f"{v['abstain']} / {v['n']} |")
    # ------------------------------------------------------------------ D. other
    cov = res.get("coverage", {})
    if cov:
        L += ["", "## D. Feature coverage (share of incidents with the gene kind observed)", "",
              "| Family/kind | sample (REAL) | sample+syn |", "|---|---|---|"]
        s_, x = cov["sample"], cov["sample+syn"]
        for f in s_["family"]:
            L.append(f"| **{f}** | {_fmt(s_['family'][f])} | {_fmt(x['family'].get(f))} |")
        for k in s_["kind"]:
            L.append(f"| {k} | {_fmt(s_['kind'][k])} | {_fmt(x['kind'].get(k))} |")
    L += ["", "## E. Runtime (wall clock)", "", "| Stage | calls | total s | mean s |", "|---|---|---|---|"]
    for k, v in res.get("runtime", {}).items():
        if isinstance(v, dict):
            L.append(f"| {k} | {v['calls']} | {v['total_s']} | {v['mean_s']} |")
    L.append(f"\nWall time: {res['runtime'].get('wall_s')} s; workers: {m.get('workers', 1)}; "
             f"extraction: {m.get('extract', 'serial')}.")
    if res["crashes"]["count"]:
        # exception class only: messages can carry organiser file names (page titles), which stay out of git
        L += ["", "## Crashes", ""] + [f"- `{c['where']}`: {c['error'].split(':')[0]}" for c in res["crashes"]["items"]]
    if made:
        L += ["", "## Charts", ""] + [f"![{f}]({f})" for f in made]
    path = out / "results.md"
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path
