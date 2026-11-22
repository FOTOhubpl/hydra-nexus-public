"""Split-conformal open-set assignment (ADVANCED_METHODS.md QW3), numpy only.

Nonconformity of item x for campaign c: α(x, c) = −support(x, c), where support is the batch scorer's
campaign-level evidence in bits for the best pair between x and a member of c (`synapse.PairEval.support`).

Calibration (`calibrate`) uses the analysed corpus:
- leave-one-incident-out: every member of a multi-member campaign is scored against the corpus without it;
  α_cal = −support(x, own campaign). These mimic "a new variant of a known campaign";
- leave-one-campaign-out: every member of a held-out multi-member campaign is scored against the other campaigns;
  the rate of empty prediction sets is the empirical rejection rate of unknown campaigns.

p-value: p_c(x) = (1 + #{α_cal ≥ α(x, c)}) / (n_cal + 1). Prediction set Γ = {c : p_c ≥ ε}, ε = 0.1.
|Γ| = 0 → new_campaign, |Γ| = 1 → assign, |Γ| ≥ 2 → abstain (analyst queue).

Honesty: the guarantee (P(own campaign ∈ Γ) ≥ 1 − ε) is marginal and assumes exchangeability between calibration
items and new items. p-values have granularity 1/(n_cal + 1): with n_cal < 1/ε − 1 nothing can ever be excluded,
and with n_cal < MIN_CALIBRATION (20) the set is reported but the support-threshold decision stays in force.
Only empirical coverage and the held-out rejection rate are reported; `calibrated=False` stays on probabilities.
"""

from __future__ import annotations

import copy
from typing import Any

import numpy as np

from hydra.contracts import AssignmentDecision, Campaign
from hydra.entropy import Rarity
from hydra.labels_pl import num_pl
from hydra.synapse import IncidentGenome, Thresholds

EPS = 0.1
MIN_CALIBRATION = 20
# Each held-out item rebuilds rarity and campaign support over the whole corpus (~1.4 s at N=825),
# so both passes are capped by a deterministic round-robin over campaigns (measured 2026-11-21 by EVAL:
# 825 incidents, 402 items, ~19 min uncapped). Corpora under the cap are unchanged.
MAX_CALIBRATION = 120
METHOD = "split_conformal@1"
SATURATION_LINKS = 2.0  # nonconformity alpha = -min(support, 2 x link)


def p_value(alpha: float, alphas_cal: np.ndarray) -> float:
    return float((1 + np.sum(alphas_cal >= alpha)) / (len(alphas_cal) + 1))


def _select(multi: list[Campaign], cap: int) -> list[tuple[Campaign, str]]:
    """Round-robin over campaigns (sorted by id), members sorted: stratified, deterministic."""
    queues = [(c, sorted(c.members)) for c in sorted(multi, key=lambda c: c.id)]
    out: list[tuple[Campaign, str]] = []
    depth = 0
    while len(out) < cap and any(depth < len(ms) for _c, ms in queues):
        for c, ms in queues:
            if depth < len(ms) and len(out) < cap:
                out.append((c, ms[depth]))
        depth += 1
    return out


def calibrate(genomes: dict[str, IncidentGenome], campaigns: list[Campaign], th: Thresholds | None = None,
              eps: float = EPS, max_items: int = MAX_CALIBRATION) -> dict[str, Any]:
    from hydra import parallel
    from hydra.pipeline import FORK_MIN_ITEMS, rarity_for

    th = th or Thresholds()
    alphas: list[float] = []
    items: list[dict[str, Any]] = []
    multi = [c for c in campaigns if len(c.members) > 1]
    total_items = sum(len(c.members) for c in multi)
    selected = _select(multi, max_items)
    # each item scores one incident against the corpus (~1 s at N=825): fork from a few dozen items on
    workers = parallel.downstream_workers(len(selected)) if len(selected) * 40 >= FORK_MIN_ITEMS else 0
    # leave-one-incident-out keeps every genome in the rarity corpus, so its rarity is the same for all items;
    # only the kit units (campaigns without the item) differ: build it once, copy it per item
    full = rarity_for(genomes) if selected else None
    owns = parallel.fork_map(_loo_item, selected, (genomes, campaigns, th, full), workers)
    for (c, m), own in zip(selected, owns, strict=True):
        alphas.append(-own)
        items.append({"incident_id": m, "campaign_id": c.id, "support_own": round(own, 3)})
    a_cal = np.array(alphas, dtype=float)
    # jackknife coverage on the calibration items themselves
    covered = [p_value(a, np.delete(a_cal, i)) >= eps for i, a in enumerate(a_cal)] if len(a_cal) > 1 else []
    # leave-one-campaign-out: empty set = correctly rejected as unknown
    partial = {"alphas": list(alphas), "eps": eps, "usable": len(a_cal) >= MIN_CALIBRATION}
    held = parallel.fork_map(_held_out_item, selected, (genomes, campaigns, th, a_cal, partial, eps), workers)
    return {
        "method": METHOD, "eps": eps, "n": len(a_cal), "alphas": [round(x, 3) for x in alphas], "items": items,
        "usable": len(a_cal) >= MIN_CALIBRATION,
        "items_total": total_items, "subsampled": total_items > len(selected),
        "empirical_coverage": round(float(np.mean(covered)), 3) if covered else None,
        "held_out_n": len(held),
        "held_out_rejection_rate": round(float(np.mean([h["set_size"] == 0 for h in held])), 3) if held else None,
        "held_out": held,
        "note_pl": ("Zbiory predykcji konformalnej; gwarancja pokrycia jest brzegowa i zakłada wymienialność."
                    f" Przy n={len(a_cal)} < {MIN_CALIBRATION} p-wartości są zgrubne (krok 1/(n+1)), więc decyzję"
                    " podejmuje próg bitów, a zbiór jest tylko raportowany."
                    if len(a_cal) < MIN_CALIBRATION else
                    "Zbiory predykcji konformalnej; gwarancja pokrycia jest brzegowa i zakłada wymienialność."),
    }


def _loo_item(shared: tuple, cm: tuple[Campaign, str]) -> float:
    """Leave-one-incident-out: support of member `m` for its own campaign `c` (pure: forked map item)."""
    from hydra.assign import campaign_supports

    genomes, campaigns, th, full = shared
    c, m = cm
    rest = [x.model_copy(update={"members": [y for y in x.members if y != m]}) for x in campaigns]
    rest = [x for x in rest if x.members]
    corpus = {k: v for k, v in genomes.items() if k != m}
    _pairs, ranked = campaign_supports(genomes[m], corpus, rest, _with_kit_units(full, rest), th)
    return next((t[0] for t in ranked if t[3] == c.id), 0.0)


def _held_out_item(shared: tuple, cm: tuple[Campaign, str]) -> dict[str, Any]:
    """Leave-one-campaign-out: member `m` of the held-out campaign `c` scored against the other campaigns."""
    from hydra.assign import ABSTAIN_MIN, MARGIN, MAX_CANDIDATES, _decide, campaign_supports
    from hydra.pipeline import rarity_for

    genomes, campaigns, th, a_cal, partial, eps = shared
    c, m = cm
    rest = [x for x in campaigns if x.id != c.id]
    corpus = {k: v for k, v in genomes.items() if k not in c.members}
    r = _rarity(rarity_for, {**corpus, m: genomes[m]}, rest)
    pairs, ranked = campaign_supports(genomes[m], corpus, rest, r, th)
    ps = [p_value(-t[0], a_cal) for t in ranked] if len(a_cal) else []
    # the open-set decision on the same scores (hydra.assign: threshold rule, then this calibration)
    res = _decide(genomes[m], ranked, pairs, {x.id: x.group_id for x in rest}, th, MARGIN, MAX_CANDIDATES,
                  ABSTAIN_MIN)
    apply_conformal(res, ranked, partial, {x.id: x.group_id for x in rest}, th)
    return {"incident_id": m, "held_campaign": c.id, "set_size": sum(p >= eps for p in ps),
            "best_support": round(ranked[0][0], 3) if ranked else 0.0,
            "decision": str(res.assignment.decision), "assigned_to": res.assignment.campaign_id}


def _with_kit_units(r: Rarity, campaigns: list[Campaign]) -> Rarity:
    """A shallow copy of `r` (postings and backgrounds shared, read-only) with campaign kit and operator units."""
    out = copy.copy(r)
    out.kit_unit_of = {m: (c.group_id or c.id) for c in campaigns for m in c.members}
    out.operator_unit_of = {m: c.id for c in campaigns for m in c.members}  # same regime as `assign` (review M1)
    out.__dict__.pop("_near_df_cache", None)  # near-hash frequencies depend on the units: no shared cache
    return out


def _rarity(rarity_for, genomes: dict[str, IncidentGenome], campaigns: list[Campaign]) -> Rarity:
    return _with_kit_units(rarity_for(genomes), campaigns)


def eligible(pe, th: Thresholds) -> bool:
    """Minimum-evidence gate that holds whatever the calibrator says (audit A12): a campaign can enter the
    prediction set only with campaign-level support of at least `low` bits, no hard contradiction, and
    operator-tier evidence or two families. The calibrator never creates evidence."""
    if pe is None or pe.score.contradictions:
        return False
    fams = sum(1 for v in pe.campaign_by_family.values() if v > 0)
    return pe.support >= max(th.link, 1e-9) and (pe.operator_bits > 0 or fams >= 2)  # review M2: link, not low


def validate_calibration(cal: dict[str, Any] | None) -> tuple[np.ndarray, float, bool, list[str]]:
    """(alphas, eps, usable, problems) derived from the calibration array itself (CR-ASTRA-5).

    A stored or externally supplied `usable` flag is never trusted: usability = every alpha is a finite number,
    there are at least MIN_CALIBRATION of them, `n` (when present) matches the array, and 0 < eps < 1. Any
    problem makes the calibration unusable, so the support-threshold decision stays in force; the problems are
    reported with the prediction set."""
    problems: list[str] = []
    cal = cal if isinstance(cal, dict) else {}
    raw = cal.get("alphas", [])
    if not isinstance(raw, (list, tuple)):
        problems.append("alphas is not a list")
        raw = []
    try:
        a_cal = np.array([float(x) for x in raw if not isinstance(x, bool)], dtype=float)
    except (TypeError, ValueError):
        problems.append("non-numeric alpha")
        a_cal = np.zeros(0, dtype=float)
    if len(a_cal) != len(raw):
        problems.append("non-numeric alpha")
    if len(a_cal) and not np.all(np.isfinite(a_cal)):
        problems.append("non-finite alpha")
        a_cal = a_cal[np.isfinite(a_cal)]
    try:
        eps = float(cal.get("eps", EPS))
    except (TypeError, ValueError):
        eps = float("nan")
    if not (0.0 < eps < 1.0):
        problems.append(f"eps outside (0, 1): {cal.get('eps')!r}")
        eps = EPS
    n = cal.get("n")
    if n is not None and n != len(raw):
        problems.append(f"n={n!r} does not match {len(raw)} alphas")
    if len(a_cal) < MIN_CALIBRATION:
        problems.append(f"{len(a_cal)} alphas < MIN_CALIBRATION={MIN_CALIBRATION}")
    return a_cal, eps, not problems, problems


def link_rule(pe, th: Thresholds) -> bool:
    """The batch same-campaign rule on one pair (`synapse.score_pair`): support >= link and operator-tier evidence
    >= OPERATOR_STRONG_BITS or two families >= STRONG_FAMILY_BITS, no contradiction."""
    from hydra.synapse import OPERATOR_STRONG_BITS, STRONG_FAMILY_BITS

    if pe is None or pe.score.contradictions:
        return False
    strong = sum(1 for v in pe.campaign_by_family.values() if v >= STRONG_FAMILY_BITS)
    return pe.support >= th.link and (pe.operator_bits >= OPERATOR_STRONG_BITS or strong >= 2)


def guarantee(n: int, eps: float) -> dict[str, Any]:
    """What the conformal output may claim (GAPS_PAPERS G04): the marginal guarantee in words and the 5-95 % band of
    the coverage conditional on this calibration set, Beta(n + 1 - l, l) with l = floor((n + 1) eps)."""
    out: dict[str, Any] = {
        "guarantee_pl": ("P(fałszywe oznaczenie jako nowy wzorzec) ≤ ε dla incydentów znanych kampanii, przy założeniu"
                         " wymienialności z próbą kalibracyjną; gwarancja brzegowa, nie dla pojedynczego incydentu."),
        "coverage_band_5_95": None}
    l_ = int(np.floor((n + 1) * eps))
    if n >= 1 and 1 <= l_ <= n:
        from scipy.stats import beta

        dist = beta(n + 1 - l_, l_)
        out["coverage_band_5_95"] = [round(float(dist.ppf(0.05)), 3), round(float(dist.ppf(0.95)), 3)]
    return out


def apply_conformal(res, ranked, cal: dict[str, Any], group_of: dict[str, str | None], th: Thresholds) -> None:
    """Attach the prediction set to an AssignResult; decide by it only when the calibration is usable (derived by
    `validate_calibration`, never read from the input). The set is intersected with the minimum-evidence gate;
    after any decision change the reason, campaign and group are rebuilt from scratch."""
    a_cal, eps, usable, problems = validate_calibration(cal)
    # Saturated nonconformity: support beyond SATURATION_LINKS x link is equally conforming. Otherwise a variant
    # with 30 bits of operator evidence looks atypical next to calibration members with 90 bits (identical files)
    # and an empty set demotes it to a new pattern (eval sample+syn, SCORING-HARDEN: 2 LOO misses at 30.5/35.2).
    sat = SATURATION_LINKS * th.link
    a_cal = np.maximum(a_cal, -sat)
    pvals = {t[3]: p_value(max(-t[0], -sat), a_cal) for t in ranked} if len(a_cal) else {}
    pe_of = {t[3]: t[4] for t in ranked}
    gated = {c for c in pvals if eligible(pe_of.get(c), th)}
    gamma = sorted((c for c, p in pvals.items() if p >= eps and c in gated), key=lambda c: -pvals[c])
    info = {"method": METHOD, "eps": eps, "n_calibration": len(a_cal), "usable": usable,
            "usable_claimed": cal.get("usable"), "calibration_problems": problems,
            "p_values": {c: round(p, 4) for c, p in pvals.items()}, "prediction_set": gamma,
            "excluded_by_evidence_gate": sorted(c for c, p in pvals.items() if p >= eps and c not in gated),
            "set_size": len(gamma), "decided_by": "threshold",
            "empirical_coverage": cal.get("empirical_coverage"),
            "held_out_rejection_rate": cal.get("held_out_rejection_rate"), "note_pl": cal.get("note_pl")}
    a = res.assignment
    info.update(guarantee(len(a_cal), eps))
    info["p_novelty"] = round(max(pvals.values()), 4) if pvals else None
    if usable:
        # Review M2: the calibrator never promotes. A one-campaign set only confirms the threshold rule's ASSIGN to
        # the same campaign (GAP-AUDIT B4: with the link rule); a larger set can only demote to ABSTAIN, an empty set
        # to NEW_CAMPAIGN; a threshold ABSTAIN or NEW_CAMPAIGN is never turned into an assignment.
        info["decided_by"] = "conformal"
        threshold_assign = a.decision == AssignmentDecision.ASSIGN
        if len(gamma) == 1 and not (threshold_assign and a.campaign_id == gamma[0] and link_rule(pe_of[gamma[0]], th)):
            info["decided_by"] = "threshold"
            info["singleton_not_confirmed"] = gamma[0]
            if not link_rule(pe_of[gamma[0]], th):
                info["singleton_below_link_rule"] = gamma[0]
        elif len(gamma) == 1:
            c = gamma[0]
            a.decision, a.campaign_id, a.group_id = AssignmentDecision.ASSIGN, c, group_of.get(c)
            pe = pe_of[c]
            a.reason_pl = (f"Zbiór predykcji konformalnej = {{{c}}} (p = {num_pl(pvals[c], '.3f')} ≥ ε ="
                           f" {num_pl(eps, 'g')}) potwierdza"
                           f" przypisanie; wsparcie {num_pl(pe.support)} bit na poziomie kampanii (n kalibracji ="
                           f" {len(a_cal)}).")
        elif len(gamma) >= 2 and a.decision == AssignmentDecision.NEW_CAMPAIGN:
            info["decided_by"] = "threshold"  # a larger set never promotes a new pattern to the analyst queue
        elif len(gamma) >= 2:
            a.decision, a.campaign_id, a.group_id = AssignmentDecision.ABSTAIN, None, None
            a.reason_pl = (f"Zbiór predykcji konformalnej zawiera {len(gamma)} kampanie ({', '.join(gamma[:3])}):"
                           f" decyzja analityka (ε = {num_pl(eps, 'g')}, n kalibracji = {len(a_cal)}).")
        else:
            gid = a.group_id if a.decision == AssignmentDecision.NEW_CAMPAIGN else None
            a.decision, a.campaign_id, a.group_id = AssignmentDecision.NEW_CAMPAIGN, None, gid
            a.reason_pl = (f"Pusty zbiór predykcji konformalnej (żadna kampania nie spełnia progu"
                           f" p ≥ ε = {num_pl(eps, 'g')} i"
                           " minimalnego dowodu): nowy wzorzec." + (f" Pasuje do grupy {gid}." if gid else ""))
    res.conformal = info
