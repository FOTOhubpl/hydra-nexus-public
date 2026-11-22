"""Grouping and assignment metrics (pure functions, no I/O)."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if p + r else 0.0


def pairwise(pred: dict[str, str], truth: dict[str, str], ignore: set | None = None) -> dict[str, float]:
    """Pairwise P/R/F1 over incidents present in both maps, plus false-merge counts. `ignore`: pairs (sorted
    tuples) left out, e.g. disputed pairs that must not count as automatic negatives."""
    ids = sorted(set(pred) & set(truth))
    tp = fp = fn = neg = 0
    for a, b in combinations(ids, 2):
        if ignore and (a, b) in ignore:
            continue
        same_p, same_t = pred[a] == pred[b], truth[a] == truth[b]
        tp += same_p and same_t
        fp += same_p and not same_t
        fn += same_t and not same_p
        neg += not same_t
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(_f1(p, r), 4), "tp": tp, "fp": fp,
            "fn": fn, "false_merge_pairs": fp, "negative_pairs": neg,
            "false_merge_rate": round(fp / neg, 4) if neg else 0.0, "n": len(ids)}


def bcubed(pred: dict[str, str], truth: dict[str, str]) -> dict[str, float]:
    ids = sorted(set(pred) & set(truth))
    if not ids:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    by_p, by_t = defaultdict(set), defaultdict(set)
    for i in ids:
        by_p[pred[i]].add(i)
        by_t[truth[i]].add(i)
    ps = rs = 0.0
    for i in ids:
        cp, ct = by_p[pred[i]], by_t[truth[i]]
        inter = len(cp & ct)
        ps += inter / len(cp)
        rs += inter / len(ct)
    p, r = ps / len(ids), rs / len(ids)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(_f1(p, r), 4)}


def cluster_purity(pred: dict[str, str], truth: dict[str, str]) -> dict[str, int]:
    by_p = defaultdict(set)
    for i, c in pred.items():
        if i in truth:
            by_p[c].add(truth[i])
    impure = sum(1 for labels in by_p.values() if len(labels) > 1)
    return {"pred_clusters": len(by_p), "true_clusters": len(set(truth[i] for i in pred if i in truth)),
            "impure_clusters": impure}


def grouping(pred: dict[str, str], truth: dict[str, str]) -> dict:
    return {"pairwise": pairwise(pred, truth), "bcubed": bcubed(pred, truth), **cluster_purity(pred, truth)}


def campaign_majority(member_of: dict[str, str], truth: dict[str, str]) -> dict[str, str | None]:
    """Predicted campaign id → majority true label of its members (ties: None)."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for i, c in member_of.items():
        if i in truth:
            votes[c][truth[i]] += 1
    out: dict[str, str | None] = {}
    for c, v in votes.items():
        top = v.most_common(2)
        out[c] = None if len(top) > 1 and top[0][1] == top[1][1] else top[0][0]
    return out


OUTCOMES = ("correct_assign", "wrong_campaign", "missed_new", "correct_new", "false_assign", "abstain")


def assignment_outcome(decision: str, assigned_label: str | None, true_label: str, label_known: bool) -> str:
    """label_known = the true label has other members in the build corpus (so 'assign' is the right answer)."""
    if decision == "abstain":
        return "abstain"
    if decision == "assign":
        if not label_known:
            return "false_assign"
        return "correct_assign" if assigned_label == true_label else "wrong_campaign"
    return "missed_new" if label_known else "correct_new"


def summarise_outcomes(rows: list[dict]) -> dict:
    c = Counter(r["outcome"] for r in rows)
    n = len(rows)
    known = sum(1 for r in rows if r["label_known"])
    unknown = n - known
    out = {k: c.get(k, 0) for k in OUTCOMES}
    out.update({
        "n": n,
        "accuracy": round((c["correct_assign"] + c["correct_new"]) / n, 4) if n else None,
        "accuracy_ci95": wilson(c["correct_assign"] + c["correct_new"], n),
        "known_accuracy_ci95": wilson(c["correct_assign"], known),
        "novel_rejection_ci95": wilson(c["correct_new"], unknown),
        "known_n": known,
        "known_accuracy": round(c["correct_assign"] / known, 4) if known else None,
        "novel_n": unknown,
        "novel_rejection": round(c["correct_new"] / unknown, 4) if unknown else None,
        "abstain_rate": round(c["abstain"] / n, 4) if n else None,
    })
    return out


def disputed_pairs(label_sets: list[dict[str, str]]) -> set[tuple[str, str]]:
    """Pairs whose same/different relation differs between label sets (sorted tuples)."""
    if len(label_sets) < 2:
        return set()
    ids = sorted(set.intersection(*(set(x) for x in label_sets)))
    out = set()
    for a, b in combinations(ids, 2):
        rel = {ls[a] == ls[b] for ls in label_sets}
        if len(rel) > 1:
            out.add((a, b))
    return out


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """Wilson 95% score interval for k successes out of n."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def bootstrap_grouping(pred: dict[str, str], truth: dict[str, str], draws: int = 1000, seed: int = 0) -> dict:
    """Campaign-level bootstrap: resample true campaigns with replacement; an incident's weight is the number
    of times its campaign was drawn. Pairs inside one campaign weigh w, pairs across campaigns w_a·w_b.
    Returns 95% percentile intervals for pairwise F1 and B-cubed F1."""
    import numpy as np

    ids = sorted(set(pred) & set(truth))
    if len(ids) < 2:
        return {}
    camps = sorted({truth[i] for i in ids})
    ci = np.array([camps.index(truth[i]) for i in ids])
    sp = np.array([[pred[a] == pred[b] for b in ids] for a in ids])
    st = ci[:, None] == ci[None, :]
    off = ~np.eye(len(ids), dtype=bool)
    rng = np.random.default_rng(seed)
    f1s, bf1s = [], []
    for _ in range(draws):
        wc = np.bincount(rng.integers(0, len(camps), len(camps)), minlength=len(camps)).astype(float)
        w = wc[ci]
        W = np.where(st, w[:, None] * (w[None, :] > 0), w[:, None] * w[None, :]) * off
        tp, fp, fn = (W * (sp & st)).sum(), (W * (sp & ~st)).sum(), (W * (~sp & st)).sum()
        p = tp / (tp + fp) if tp + fp else 1.0
        r = tp / (tp + fn) if tp + fn else 1.0
        f1s.append(2 * p * r / (p + r) if p + r else 0.0)
        # B-cubed with incident weights (self included)
        Wb = w[None, :] * (w[:, None] > 0)
        bp = ((Wb * (sp & st)).sum(1) / np.maximum((Wb * sp).sum(1), 1e-12))
        br = ((Wb * (sp & st)).sum(1) / np.maximum((Wb * st).sum(1), 1e-12))
        P, R = (bp * w).sum() / w.sum(), (br * w).sum() / w.sum()
        bf1s.append(2 * P * R / (P + R) if P + R else 0.0)
    q = lambda xs: [round(float(np.percentile(xs, 2.5)), 4), round(float(np.percentile(xs, 97.5)), 4)]  # noqa: E731
    return {"pairwise_f1_ci95": q(f1s), "bcubed_f1_ci95": q(bf1s), "draws": draws, "unit": "campaign",
            "campaigns": len(camps)}


def connectivity_recall(candidates: set[tuple[str, str]], truth: dict[str, str]) -> dict:
    """Share of labelled multi-member campaigns whose members stay connected inside the candidate graph."""
    by: dict[str, list[str]] = defaultdict(list)
    for i, lab in truth.items():
        by[lab].append(i)
    multi = {lab: m for lab, m in by.items() if len(m) > 1}
    ok = 0
    for members in multi.values():
        ms = set(members)
        adj: dict[str, set[str]] = defaultdict(set)
        for a, b in candidates:
            if a in ms and b in ms:
                adj[a].add(b)
                adj[b].add(a)
        seen, stack = {members[0]}, [members[0]]
        while stack:
            for nb in adj[stack.pop()] - seen:
                seen.add(nb)
                stack.append(nb)
        ok += len(seen) == len(ms)
    rec = round(ok / len(multi), 4) if multi else None
    return {"campaigns": len(multi), "connected": ok, "connectivity_recall": rec}


def partition_stability(default: dict[str, str], perturbed: list[dict[str, str]], jaccard: float = 0.8) -> dict:
    """Per default multi-member campaign: share of perturbations whose best-matching cluster has Jaccard ≥ 0.8;
    plus AMI of each perturbed partition against the default."""
    from sklearn.metrics import adjusted_mutual_info_score

    ids = sorted(default)
    camps: dict[str, set[str]] = defaultdict(set)
    for i in ids:
        camps[default[i]].add(i)
    multi = {c: m for c, m in camps.items() if len(m) > 1}
    stab = {c: 0 for c in multi}
    amis = []
    for p in perturbed:
        clus: dict[str, set[str]] = defaultdict(set)
        for i in ids:
            clus[p.get(i, f"missing-{i}")].add(i)
        for c, m in multi.items():
            best = max((len(m & s) / len(m | s) for s in clus.values()), default=0.0)
            stab[c] += best >= jaccard
        amis.append(adjusted_mutual_info_score([default[i] for i in ids], [p.get(i, f"m-{i}") for i in ids]))
    n = max(1, len(perturbed))
    per = {c: round(v / n, 3) for c, v in stab.items()}
    return {"perturbations": len(perturbed), "campaigns": len(multi),
            "unstable_campaigns_lt_0_5": sum(1 for v in per.values() if v < 0.5),
            "mean_stability": round(sum(per.values()) / len(per), 4) if per else None,
            "ami_min": round(min(amis), 4) if amis else None, "ami_mean": round(sum(amis) / len(amis), 4) if amis
            else None, "per_campaign_stability_values": sorted(per.values())}
