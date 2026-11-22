"""Display-only reliability of pair support bits (EVAL-EXTRA, 2026-11-22). Measurement only: no scoring change.

Fits an isotonic map support bits -> P(same campaign) on the pairs of one campaign-disjoint half of a labelled
corpus and evaluates it on the other half (Brier score, ECE with 10 equal-width bins, reliability chart). The
product keeps deciding on bits (`synapse.Thresholds.link`); this calibration is for display only
("kalibracja wyświetlana, decyzje nadal na bitach").

Input: a pairs JSONL written by `eval.threshold_sweep --dump-pairs` (one row per scored candidate pair:
`support`, `probability` (the product's uncalibrated logistic of bits), `label_a`, `label_b`; ids hashed).

    python -m eval.calibration --pairs pairs.jsonl --json out.json --png docs/eval/reliability.png --splits 5
Prints aggregates only.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

DISPLAY_NOTE_PL = "kalibracja wyświetlana, decyzje nadal na bitach"


def brier(p: list[float], y: list[int]) -> float:
    return sum((a - b) ** 2 for a, b in zip(p, y, strict=True)) / len(y) if y else 0.0


def reliability_bins(p: list[float], y: list[int], bins: int = 10) -> list[dict]:
    """Equal-width bins on [0, 1]; the last bin includes 1.0. Empty bins are reported with n=0."""
    out = [{"lo": k / bins, "hi": (k + 1) / bins, "n": 0, "mean_p": None, "frac_pos": None} for k in range(bins)]
    acc = [[0, 0.0, 0] for _ in range(bins)]
    for pi, yi in zip(p, y, strict=True):
        k = min(bins - 1, max(0, int(pi * bins)))
        acc[k][0] += 1
        acc[k][1] += pi
        acc[k][2] += yi
    for k, (n, sp, sy) in enumerate(acc):
        if n:
            out[k].update(n=n, mean_p=round(sp / n, 4), frac_pos=round(sy / n, 4))
    return out


def ece(p: list[float], y: list[int], bins: int = 10) -> float:
    n = len(y)
    if not n:
        return 0.0
    return sum(b["n"] / n * abs(b["mean_p"] - b["frac_pos"]) for b in reliability_bins(p, y, bins) if b["n"])


def fit_isotonic(x: list[float], y: list[int]):
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
    iso.fit(x, y)
    return iso


def campaign_split(labels: list[str], seed: int) -> tuple[set[str], set[str]]:
    """Split the distinct campaign labels in two halves (deterministic for `seed`)."""
    uniq = sorted(set(labels))
    rng = random.Random(seed)
    rng.shuffle(uniq)
    half = len(uniq) // 2
    return set(uniq[:half]), set(uniq[half:])


def split_pairs(rows: list[dict], side: set[str]) -> list[dict]:
    """Pairs whose two incidents both belong to campaigns of `side` (cross-half pairs are dropped)."""
    return [r for r in rows if r["label_a"] in side and r["label_b"] in side]


def _xy(rows: list[dict]) -> tuple[list[float], list[int]]:
    return [float(r["support"]) for r in rows], [int(r["label_a"] == r["label_b"]) for r in rows]


def evaluate_split(rows: list[dict], seed: int, bins: int = 10) -> dict | None:
    """Fit on half A, evaluate on half B (campaign-disjoint). Returns None when a half has no positive or no
    negative pair (isotonic fit / Brier on one class says nothing)."""
    labels = [r["label_a"] for r in rows] + [r["label_b"] for r in rows]
    a_side, b_side = campaign_split(labels, seed)
    fit_rows, test_rows = split_pairs(rows, a_side), split_pairs(rows, b_side)
    xf, yf = _xy(fit_rows)
    xt, yt = _xy(test_rows)
    if not (0 < sum(yf) < len(yf)) or not (0 < sum(yt) < len(yt)):
        return None
    iso = fit_isotonic(xf, yf)
    p_iso = [float(v) for v in iso.predict(xt)]
    p_raw = [float(r["probability"]) for r in test_rows]
    return {"seed": seed, "fit_campaigns": len(a_side), "test_campaigns": len(b_side),
            "fit_pairs": len(fit_rows), "fit_positive": sum(yf), "test_pairs": len(test_rows),
            "test_positive": sum(yt),
            "isotonic": {"brier": round(brier(p_iso, yt), 4), "ece": round(ece(p_iso, yt, bins), 4),
                         "bins": reliability_bins(p_iso, yt, bins)},
            "uncalibrated_logistic": {"brier": round(brier(p_raw, yt), 4), "ece": round(ece(p_raw, yt, bins), 4),
                                      "bins": reliability_bins(p_raw, yt, bins)},
            "map": [[round(float(a), 3), round(float(b), 4)]
                    for a, b in zip(iso.X_thresholds_, iso.y_thresholds_, strict=True)]}


def run(rows: list[dict], splits: int, bins: int = 10) -> dict:
    res = [r for s in range(splits) if (r := evaluate_split(rows, s, bins)) is not None]

    def mean(path: tuple[str, str]) -> float | None:
        v = [r[path[0]][path[1]] for r in res]
        return round(sum(v) / len(v), 4) if v else None

    return {"note_pl": DISPLAY_NOTE_PL, "pairs": len(rows),
            "positive_pairs": sum(int(r["label_a"] == r["label_b"]) for r in rows),
            "campaigns": len({r["label_a"] for r in rows} | {r["label_b"] for r in rows}),
            "splits_requested": splits, "splits_used": len(res),
            "mean": {"isotonic_brier": mean(("isotonic", "brier")), "isotonic_ece": mean(("isotonic", "ece")),
                     "logistic_brier": mean(("uncalibrated_logistic", "brier")),
                     "logistic_ece": mean(("uncalibrated_logistic", "ece"))},
            "per_split": res}


def pooled_bins(splits: list[dict], key: str) -> list[tuple[float, float, int]]:
    """(mean predicted p, observed positive share, pairs) per bin, pooled over the test halves of all splits."""
    acc: dict[int, list[float]] = {}
    for s in splits:
        for k, b in enumerate(s[key]["bins"]):
            if b["n"]:
                a = acc.setdefault(k, [0.0, 0.0, 0.0])
                a[0] += b["n"]
                a[1] += b["mean_p"] * b["n"]
                a[2] += b["frac_pos"] * b["n"]
    return [(a[1] / a[0], a[2] / a[0], int(a[0])) for _k, a in sorted(acc.items())]


def chart(result: dict, png: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    splits = result["per_split"]
    if not splits:
        return
    fig, ax = plt.subplots(figsize=(6.4, 5.8), dpi=150)
    ax.plot([0, 1], [0, 1], color="#9a9893", lw=1, ls="--", label="idealna kalibracja")
    mean = result["mean"]
    for key, mk, color, name in (("isotonic", "isotonic", "#2a78d6", "izotoniczna (dopasowana na połowie A)"),
                                 ("uncalibrated_logistic", "logistic", "#eb6834", "logistyka bitów (bez kalibracji)")):
        pts = pooled_bins(splits, key)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, lw=2, zorder=2,
                label=f"{name}: Brier {mean[mk + '_brier']:.3f}, ECE {mean[mk + '_ece']:.3f}")
        ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=[max(16.0, min(220.0, p[2] / 8)) for p in pts],
                   color=color, edgecolors="#fcfcfb", linewidths=1.5, zorder=3)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel("przewidywane P(ta sama kampania)")
    ax.set_ylabel("odsetek par z tej samej kampanii (połowa B)")
    ax.set_title(f"{title}\n{DISPLAY_NOTE_PL}", fontsize=10)
    ax.grid(color="#e6e5e0", lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="lower right", fontsize=7.5, frameon=False)
    n_test = sum(s["test_pairs"] for s in splits)
    fig.text(0.01, 0.01, f"Brier/ECE: średnia z {len(splits)} podziałów rozłącznych kampanii. Punkty: przedziały co 0,1"
             f" z par testowych\nwszystkich podziałów ({n_test} par łącznie, para może wystąpić w kilku podziałach);"
             f" wielkość punktu ~ liczba par.", fontsize=6.5, color="#52514e")
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", type=Path, default=None)
    ap.add_argument("--from-json", type=Path, default=None, help="re-render the chart from a saved --json result")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--png", type=Path, default=None)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--title", default="Wiarygodność bitów wsparcia pary (zbiór syntetyczny)")
    a = ap.parse_args()
    if a.from_json:
        chart(json.loads(a.from_json.read_text(encoding="utf-8")), a.png, a.title)
        return
    rows = [json.loads(line) for line in a.pairs.open(encoding="utf-8") if line.strip()]
    res = run(rows, a.splits)
    if a.png:
        chart(res, a.png, a.title)
    out = {k: v for k, v in res.items() if k != "per_split"}
    out["per_split"] = [{k: v for k, v in s.items() if k not in ("map",)} for s in res["per_split"]]
    print(json.dumps({k: v for k, v in out.items() if k != "per_split"}, indent=1, ensure_ascii=False))
    for s in res["per_split"]:
        print(f"split {s['seed']}: fit {s['fit_pairs']} ({s['fit_positive']} pos) test {s['test_pairs']}"
              f" ({s['test_positive']} pos)  iso brier {s['isotonic']['brier']} ece {s['isotonic']['ece']}"
              f"  logistic brier {s['uncalibrated_logistic']['brier']} ece {s['uncalibrated_logistic']['ece']}")
    if a.json:
        a.json.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
