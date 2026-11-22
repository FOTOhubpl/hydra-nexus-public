"""Pair scorers for the baseline ladder (B0 pHash, B1 text, B2 naive sum, B3 rarity bits, LR) plus
clustering (connected components) and open-set assignment built on any scorer.

They work on genes (hydra.contracts.Gene) from whichever extractor ran, so the same ladder can be applied to
CORE's genes once they exist. Scores are heuristic: B3 produces evidence bits (not probabilities), LR a
probability that is only as good as the tiny label set it was fitted on (reported as uncalibrated).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

import numpy as np

from hydra.contracts import (
    Assignment,
    AssignmentCandidate,
    AssignmentDecision,
    Campaign,
    Family,
    Gene,
    GeneMatch,
    GeneStatus,
    Hypothesis,
    PairScore,
)

FAMILIES = [str(f) for f in Family]
SET_KINDS = {"txt_shingle": 12.0, "dom_shingle": 8.0}  # Jaccard kinds and their bit caps
NEAR_KINDS = {"phash": 8}  # Hamming radius for near-duplicate hashes (64-bit pHash)
FAMILY_CAP = {"VIS": 16.0, "TXT": 16.0, "INF": 20.0, "ID": 12.0, "NAR": 2.0, "META": 12.0}
KIND_CAP = 14.0
DUPLICATE_JACCARD = 0.8  # share of identical files (sha256) above which two incidents are duplicates


@dataclass
class Profile:
    iid: str
    values: dict[str, set[str]] = field(default_factory=dict)
    family_of: dict[str, str] = field(default_factory=dict)
    group_of: dict[str, str] = field(default_factory=dict)
    observed_families: set[str] = field(default_factory=set)


def profile(iid: str, genes: list[Gene], drop_families: set[str] | frozenset = frozenset()) -> Profile:
    p = Profile(iid)
    for g in genes:
        fam = str(g.family)
        if fam in drop_families or str(g.status) != GeneStatus.OBSERVED or g.value is None:
            continue
        p.values.setdefault(g.kind, set()).add(g.value)
        p.family_of[g.kind] = fam
        p.group_of[g.kind] = g.dependency_group or g.kind
        p.observed_families.add(fam)
    return p


def _hex64(values: set[str]) -> np.ndarray:
    out = []
    for v in sorted(values):  # sorted: set order of str varies with PYTHONHASHSEED, matching must not
        try:
            out.append(int(v, 16) & 0xFFFFFFFFFFFFFFFF)
        except ValueError:
            continue
    return np.array(out, dtype=np.uint64)


def _hamming(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.bitwise_count(np.bitwise_xor(a[:, None], b[None, :]))


def near_matches(a: set[str], b: set[str], radius: int) -> list[tuple[str, str, int]]:
    ha, hb = _hex64(a), _hex64(b)
    if not len(ha) or not len(hb):
        return []
    d = _hamming(ha, hb)
    out = []
    used_b: set[int] = set()
    for i in range(d.shape[0]):
        j = int(d[i].argmin())
        if d[i, j] <= radius and j not in used_b:
            used_b.add(j)
            out.append((format(int(ha[i]), "016x"), format(int(hb[j]), "016x"), int(d[i, j])))
    return out


# --------------------------------------------------------------------------------------- base


class Scorer:
    name = "base"
    model = "base@1"
    threshold = 0.5
    grid: list[float] = []

    def fit(self, profiles: dict[str, Profile], labels: dict[str, str] | None = None) -> None:
        self.profiles = profiles

    def score(self, a: Profile, b: Profile) -> PairScore:  # pragma: no cover - abstract
        raise NotImplementedError

    def _pair(self, a: Profile, b: Profile, total: float, fam_bits: dict[str, float], reasons: list[GeneMatch],
              prob: float | None = None) -> PairScore:
        x, y = sorted([a.iid, b.iid])
        missing = [f for f in FAMILIES if f not in a.observed_families or f not in b.observed_families]
        if prob is None:
            prob = 1.0 / (1.0 + math.exp(-(total - self.threshold)))
        if total >= self.threshold:
            hyp = Hypothesis.SAME_CAMPAIGN
        elif fam_bits and set(k for k, v in fam_bits.items() if v > 0) == {"VIS"}:
            hyp = Hypothesis.SHARED_ASSET
        else:
            hyp = Hypothesis.INDEPENDENT
        reasons = sorted(reasons, key=lambda r: -abs(r.bits))[:12]
        return PairScore(a=x, b=y, family_bits={k: round(v, 3) for k, v in fam_bits.items()},
                         missing_families=missing, reasons=reasons, total_bits=round(total, 4),
                         probability=min(1.0, max(0.0, prob)), calibrated=False, hypothesis=hyp, model=self.model)


class PHashOnly(Scorer):
    """B0: link when any image/frame pHash is within Hamming radius; score = 1 - d_min/64."""

    name, model, threshold = "B0_phash", "eval_b0_phash@1", 0.9
    grid = [0.8, 0.85, 0.875, 0.9, 0.92, 0.95, 0.97, 1.0]

    def score(self, a: Profile, b: Profile) -> PairScore:
        m = near_matches(a.values.get("phash", set()), b.values.get("phash", set()), 64)
        if not m:
            return self._pair(a, b, 0.0, {}, [])
        best = min(m, key=lambda t: t[2])
        s = 1 - best[2] / 64
        r = GeneMatch(family=Family.VIS, kind="phash", value_a=best[0], value_b=best[1], similarity=s, bits=s,
                      explanation_pl=f"Najbliższy pHash: odległość Hamminga {best[2]}")
        return self._pair(a, b, s, {"VIS": s}, [r], prob=s)


class TextOnly(Scorer):
    """B1: max of text-shingle Jaccard, exact title match (1.0), headline overlap."""

    name, model, threshold = "B1_text", "eval_b1_text@1", 0.3
    grid = [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 0.9]

    TEXT_SETS = ("txt_shingle", "phrase", "minhash_band")      # stub kinds / CORE kinds
    TITLES = ("title", "page_title", "ad_headline")

    def score(self, a: Profile, b: Profile) -> PairScore:
        sa = set().union(*(a.values.get(k, set()) for k in self.TEXT_SETS))
        sb = set().union(*(b.values.get(k, set()) for k in self.TEXT_SETS))
        j = len(sa & sb) / len(sa | sb) if sa and sb else 0.0
        ta = set().union(*(a.values.get(k, set()) for k in self.TITLES))
        tb = set().union(*(b.values.get(k, set()) for k in self.TITLES))
        t = 1.0 if ta & tb else 0.0
        ha, hb = a.values.get("headline", set()), b.values.get("headline", set())
        h = len(ha & hb) / min(len(ha), len(hb)) if ha and hb else 0.0
        s = max(j, t, h)
        r = GeneMatch(family=Family.TXT, kind="text", similarity=s, bits=s,
                      explanation_pl=f"Tekst: Jaccard {j:.2f}, tytuł {'zgodny' if t else 'różny'}, nagłówki {h:.2f}")
        return self._pair(a, b, s, {"TXT": s}, [r], prob=s)


class NaiveSum(Scorer):
    """B2: count of shared gene values over all kinds (no rarity, no dependency groups)."""

    name, model, threshold = "B2_naive_sum", "eval_b2_naive@1", 20.0
    grid = [2, 5, 10, 20, 40, 80, 160, 320]

    def score(self, a: Profile, b: Profile) -> PairScore:
        total, fam = 0.0, defaultdict(float)
        for kind in set(a.values) & set(b.values):
            if kind in NEAR_KINDS:
                n = len(near_matches(a.values[kind], b.values[kind], NEAR_KINDS[kind]))
            else:
                n = len(a.values[kind] & b.values[kind])
            total += n
            fam[a.family_of[kind]] += n
        return self._pair(a, b, total, dict(fam), [], prob=min(1.0, total / (2 * self.threshold)))


class RarityBits(Scorer):
    """B3: rarity-weighted evidence bits, joint per dependency group and family (PLAN_MAX §1.3).

    bits(value) = log2(N_eff / df), N_eff = max(N, 64) because a tiny corpus would make every value look common.
    Values in more than max(2, 30% of N) incidents are stop-genes (0 bits). Within a kind: best value + 25% of
    the rest; within a dependency group: best kind + 25% of the rest; within a family: best group + 50% of the
    rest; family caps bound any one modality. Missing families contribute 0 (unknown, never negative).
    """

    name, model, threshold = "B3_rarity_bits", "eval_b3_rarity@1", 10.0
    grid = [4, 6, 8, 10, 12, 14, 16, 20, 24, 30]

    def __init__(self, drop_families: set[str] | frozenset = frozenset()) -> None:
        self.drop = set(drop_families)

    def fit(self, profiles: dict[str, Profile], labels: dict[str, str] | None = None) -> None:
        self.profiles = profiles
        self.n = max(1, len(profiles))
        self.df: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for p in profiles.values():
            for kind, vals in p.values.items():
                if kind in NEAR_KINDS:
                    continue
                for v in vals:
                    self.df[kind][v] += 1
        lim = max(2, 0.5 * self.n)
        self.stop = {k: {v for v, c in self.df.get(k, {}).items() if c > lim} for k in SET_KINDS}
        # near-duplicate df for hashes: number of incidents holding a hash within the radius (vectorised)
        self.near_df: dict[str, dict[str, int]] = {}
        for kind, rad in NEAR_KINDS.items():
            hs, owner = [], []
            for k, p in enumerate(profiles.values()):
                h = _hex64(p.values.get(kind, set()))
                hs.append(h)
                owner += [k] * len(h)
            counts: dict[str, int] = {}
            if owner:
                allh = np.concatenate(hs)
                own = np.array(owner)
                close = _hamming(allh, allh) <= rad
                for i in range(len(allh)):
                    counts[format(int(allh[i]), "016x")] = len(set(own[close[i]].tolist()))
            self.near_df[kind] = counts

    def _bits(self, df: int) -> float:
        n_eff = max(self.n, 64)
        if df > max(2, 0.3 * self.n):
            return 0.0
        return math.log2(n_eff / max(1, df))

    def score(self, a: Profile, b: Profile) -> PairScore:
        kind_bits: dict[str, float] = {}
        reasons: list[GeneMatch] = []
        for kind in set(a.values) & set(b.values):
            fam = a.family_of[kind]
            if fam in self.drop:
                continue
            va, vb = a.values[kind], b.values[kind]
            if kind in SET_KINDS:
                stop = self.stop.get(kind, set())
                fa, fb = va - stop, vb - stop
                if not fa or not fb:
                    continue
                j = len(fa & fb) / len(fa | fb)
                if j < 0.05:
                    continue
                kb = SET_KINDS[kind] * j
                kind_bits[kind] = kb
                reasons.append(GeneMatch(family=fam, kind=kind, similarity=round(j, 3), bits=round(kb, 3),
                                         explanation_pl=f"Wspólne fragmenty ({kind}): Jaccard {j:.2f}"))
                continue
            if kind in NEAR_KINDS:
                matches = near_matches(va, vb, NEAR_KINDS[kind])
                vals = []
                for x, y, d in matches:
                    df = self.near_df.get(kind, {}).get(x, 2)
                    bits = self._bits(df) * (1 - d / (2 * NEAR_KINDS[kind] + 1))
                    vals.append(bits)
                    if bits > 0:
                        reasons.append(GeneMatch(family=fam, kind=kind, value_a=x, value_b=y, similarity=1 - d / 64,
                                                 doc_freq=df, bits=round(bits, 3),
                                                 explanation_pl=f"Prawie identyczny obraz (Hamming {d}), "
                                                                f"występuje w {df} incydentach"))
            else:
                vals = []
                for v in va & vb:
                    df = self.df.get(kind, {}).get(v, 2)
                    bits = self._bits(df)
                    vals.append(bits)
                    if bits > 0:
                        reasons.append(GeneMatch(family=fam, kind=kind, value_a=v, value_b=v, doc_freq=df,
                                                 bits=round(bits, 3),
                                                 explanation_pl=f"Wspólna wartość {kind}: {v[:60]} "
                                                                f"(w {df} incydentach)"))
            vals = sorted((v for v in vals if v > 0), reverse=True)
            if vals:
                kind_bits[kind] = min(KIND_CAP, vals[0] + 0.25 * sum(vals[1:]))
        # dependency groups → families
        groups: dict[tuple[str, str], list[float]] = defaultdict(list)
        for kind, kb in kind_bits.items():
            groups[(a.family_of[kind], a.group_of.get(kind, kind))].append(kb)
        fam_groups: dict[str, list[float]] = defaultdict(list)
        for (fam, _g), kbs in groups.items():
            kbs.sort(reverse=True)
            fam_groups[fam].append(kbs[0] + 0.25 * sum(kbs[1:]))
        fam_bits = {}
        for fam, gb in fam_groups.items():
            gb.sort(reverse=True)
            fam_bits[fam] = min(FAMILY_CAP.get(fam, 12.0), gb[0] + 0.5 * sum(gb[1:]))
        total = sum(fam_bits.values())
        # duplicate rule: (nearly) the same set of original files = the same material, whatever the rarity
        sa = a.values.get("asset_sha256", set())
        sb = b.values.get("asset_sha256", set())
        if len(sa) >= 1 and len(sb) >= 1 and "VIS" not in self.drop and "INF" not in self.drop:
            jac = len(sa & sb) / len(sa | sb)
            if jac >= DUPLICATE_JACCARD:
                total = max(total, self.threshold + 1.0)
                reasons.append(GeneMatch(family=Family.VIS, kind="duplicate_files", similarity=round(jac, 3),
                                         bits=round(total, 3), explanation_pl=f"Duplikat: {jac:.0%} identycznych "
                                         "plików (sha256)"))
        return self._pair(a, b, total, fam_bits, reasons)


class LogisticFusion(Scorer):
    """LR on B3 per-family bits + per-family missingness, fitted on labelled pairs of the build corpus.

    Only used when the build corpus has >= 3 positive and >= 3 negative pairs; otherwise it falls back to B3
    and reports `lr_fallback`. Class-balanced, L2; probability is NOT calibrated on a held-out set.
    """

    name, model, threshold = "LR_fusion", "eval_lr_bits@1", 0.5
    grid = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99]

    def __init__(self) -> None:
        self.base = RarityBits()
        self.clf = None
        self.fallback = False

    def _x(self, ps: PairScore) -> list[float]:
        return [ps.family_bits.get(f, 0.0) for f in FAMILIES] + [1.0 if f in ps.missing_families else 0.0
                                                                   for f in FAMILIES]

    def fit(self, profiles: dict[str, Profile], labels: dict[str, str] | None = None) -> None:
        from sklearn.linear_model import LogisticRegression

        self.profiles = profiles
        self.base.fit(profiles)
        labels = labels or {}
        xs, ys = [], []
        ids = [i for i in profiles if labels.get(i)]
        for i, j in combinations(ids, 2):
            xs.append(self._x(self.base.score(profiles[i], profiles[j])))
            ys.append(1 if labels[i] == labels[j] else 0)
        if sum(ys) >= 3 and len(ys) - sum(ys) >= 3:
            self.clf = LogisticRegression(C=1.0, class_weight="balanced", max_iter=1000).fit(xs, ys)
            self.fallback = False
        else:
            self.clf, self.fallback = None, True

    def score(self, a: Profile, b: Profile) -> PairScore:
        ps = self.base.score(a, b)
        if self.clf is None:
            p = 1.0 / (1.0 + math.exp(-(ps.total_bits - self.base.threshold)))
            model = "eval_lr_bits@1(fallback=B3)"
        else:
            p = float(self.clf.predict_proba([self._x(ps)])[0, 1])
            model = self.model
        out = self._pair(a, b, p, ps.family_bits, list(ps.reasons), prob=p)
        out.model = model
        out.total_bits = ps.total_bits
        out.hypothesis = Hypothesis.SAME_CAMPAIGN if p >= self.threshold else out.hypothesis
        return out

    def decision_value(self, ps: PairScore) -> float:
        return ps.probability


SCORERS = {c.name: c for c in (PHashOnly, TextOnly, NaiveSum, RarityBits, LogisticFusion)}


def decision_value(scorer: Scorer, ps: PairScore) -> float:
    return ps.probability if isinstance(scorer, LogisticFusion) else ps.total_bits


# --------------------------------------------------------------------------------------- build / assign


@dataclass
class Build:
    ids: list[str]
    pairs: dict[tuple[str, str], PairScore]
    campaigns: list[Campaign]
    member_of: dict[str, str]
    model: str
    group_of: dict[str, str] = field(default_factory=dict)  # incident -> predicted campaign group id
    ctx: object = None  # backend state needed by assign (fitted scorer, CORE handle)


def connected_components(ids: list[str], links: list[tuple[str, str]]) -> list[list[str]]:
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    comps: dict[str, list[str]] = defaultdict(list)
    for i in ids:
        comps[find(i)].append(i)
    return sorted((sorted(c) for c in comps.values()), key=lambda c: c[0])


def build(scorer: Scorer, profiles: dict[str, Profile], labels: dict[str, str] | None = None) -> Build:
    scorer.fit(profiles, labels)
    ids = sorted(profiles)
    pairs: dict[tuple[str, str], PairScore] = {}
    links = []
    for i, j in combinations(ids, 2):
        ps = scorer.score(profiles[i], profiles[j])
        pairs[(ps.a, ps.b)] = ps
        if decision_value(scorer, ps) >= scorer.threshold:
            links.append((ps.a, ps.b))
    comps = connected_components(ids, links)
    gthr = group_threshold(scorer)
    glinks = [k for k, ps in pairs.items() if decision_value(scorer, ps) >= gthr] + links
    gcomps = connected_components(ids, glinks)
    group_of = {m: f"g-{g[0]}" for g in gcomps for m in g}
    camps, member_of = [], {}
    for c in comps:
        cid = f"c-{c[0]}"
        extra = {"group_id": group_of[c[0]]} if "group_id" in Campaign.model_fields else {}
        camps.append(Campaign(id=cid, label=f"Kampania {c[0]} ({len(c)})", members=c,
                              confidence=1.0 if len(c) > 1 else 0.0, **extra))
        for m in c:
            member_of[m] = cid
    return Build(ids=ids, pairs=pairs, campaigns=camps, member_of=member_of, model=scorer.model,
                 group_of=group_of, ctx=scorer)


def group_threshold(scorer: Scorer) -> float:
    """Kit-level (campaign group) link threshold: an explicit `group_threshold` or 60% of the campaign one."""
    return getattr(scorer, "group_threshold", None) or 0.6 * scorer.threshold


def assign(scorer: Scorer, b: Build, new: Profile, margin: float = 0.0) -> Assignment:
    """Score `new` against every member (rarity stats of the build corpus); best campaign = max pair."""
    best: dict[str, tuple[float, PairScore, int]] = {}
    for camp in b.campaigns:
        top, top_ps, support = -1e9, None, 0
        for m in camp.members:
            ps = scorer.score(new, scorer.profiles[m])
            v = decision_value(scorer, ps)
            if v >= scorer.threshold:
                support += 1
            if v > top:
                top, top_ps = v, ps
        best[camp.id] = (top, top_ps, support)
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
    cands = [AssignmentCandidate(campaign_id=cid, probability=float(ps.probability) if ps else 0.0, best_pair=ps,
                                 support=sup) for cid, (v, ps, sup) in ranked[:5]]
    camp_group = {c.id: getattr(c, "group_id", None) or b.group_of.get(c.members[0]) for c in b.campaigns}
    has_gid = "group_id" in Assignment.model_fields
    if not ranked or ranked[0][1][0] < scorer.threshold:
        top = ranked[0][1][0] if ranked else 0.0
        gid = camp_group.get(ranked[0][0]) if ranked and top >= group_threshold(scorer) else None
        why = f"; pasuje do grupy {gid}" if gid else ""
        return Assignment(incident_id=new.iid, decision=AssignmentDecision.NEW_CAMPAIGN, candidates=cands,
                          model=scorer.model, **({"group_id": gid} if has_gid else {}),
                          reason_pl=f"Brak kampanii powyżej progu ({top:.2f} < {scorer.threshold}){why}")
    if len(ranked) > 1 and ranked[1][1][0] >= scorer.threshold and ranked[0][1][0] - ranked[1][1][0] <= margin:
        return Assignment(incident_id=new.iid, decision=AssignmentDecision.ABSTAIN, candidates=cands,
                          reason_pl="Dwie kampanie z podobnym wsparciem", model=scorer.model)
    cid, (v, _ps, sup) = ranked[0]
    return Assignment(incident_id=new.iid, decision=AssignmentDecision.ASSIGN, campaign_id=cid, candidates=cands,
                      **({"group_id": camp_group.get(cid)} if has_gid else {}),
                      reason_pl=f"Najsilniejsze powiązanie {v:.2f} (próg {scorer.threshold}), wsparcie {sup}",
                      model=scorer.model)
