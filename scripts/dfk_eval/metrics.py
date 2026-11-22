"""Shared metrics for the DFK evaluation scripts (server only; aggregates only, no media written to git).

AUROC with a percentile bootstrap CI (stratified resampling of each class), TPR at a fixed FPR (threshold taken
from the negatives' empirical quantile) and the FPR/TPR at a given threshold.
"""

from __future__ import annotations

import numpy as np


def auroc(neg: np.ndarray, pos: np.ndarray) -> float:
    """P(score_pos > score_neg) + 0.5 P(tie) (Mann-Whitney), higher score = positive."""
    neg, pos = np.asarray(neg, float), np.asarray(pos, float)
    if len(neg) == 0 or len(pos) == 0:
        return float("nan")
    allv = np.concatenate([neg, pos])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    ranks[order] = np.arange(1, len(allv) + 1)
    # average ranks for ties
    _, inv, cnt = np.unique(allv, return_inverse=True, return_counts=True)
    sums = np.bincount(inv, weights=ranks)
    ranks = (sums / cnt)[inv]
    r_pos = ranks[len(neg):].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def tpr_at_fpr(neg: np.ndarray, pos: np.ndarray, fpr: float) -> tuple[float, float]:
    """(TPR, threshold) with threshold = the (1 - fpr) quantile of the negatives (score > thr is positive)."""
    thr = float(np.quantile(np.asarray(neg, float), 1.0 - fpr))
    return float((np.asarray(pos, float) > thr).mean()), thr


def bootstrap(neg: np.ndarray, pos: np.ndarray, fn, n: int = 1000, seed: int = 0) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    neg, pos = np.asarray(neg, float), np.asarray(pos, float)
    vals = [fn(neg[rng.integers(0, len(neg), len(neg))], pos[rng.integers(0, len(pos), len(pos))])
            for _ in range(n)]
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def summary(neg, pos, fprs=(0.01, 0.05), seed: int = 0) -> dict:
    neg, pos = np.asarray(neg, float), np.asarray(pos, float)
    out = {"n_neg": int(len(neg)), "n_pos": int(len(pos)), "auroc": round(auroc(neg, pos), 4)}
    lo, hi = bootstrap(neg, pos, auroc, seed=seed)
    out["auroc_ci95"] = [round(lo, 4), round(hi, 4)]
    for f in fprs:
        t, thr = tpr_at_fpr(neg, pos, f)
        lo, hi = bootstrap(neg, pos, lambda a, b, f=f: tpr_at_fpr(a, b, f)[0], seed=seed)
        out[f"tpr_at_fpr{f:g}"] = {"tpr": round(t, 4), "ci95": [round(lo, 4), round(hi, 4)], "thr": thr}
    out["neg_quantiles"] = {q: float(np.quantile(neg, q)) for q in (0.5, 0.9, 0.95, 0.99)} if len(neg) else {}
    out["pos_quantiles"] = {q: float(np.quantile(pos, q)) for q in (0.1, 0.5, 0.9)} if len(pos) else {}
    return out


def rate_at(scores, thr: float) -> float:
    s = np.asarray(scores, float)
    return float((s > thr).mean()) if len(s) else float("nan")
