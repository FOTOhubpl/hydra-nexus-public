"""M9 ASSIGN: open-set assignment of a new incident to a known campaign, a new pattern, or abstain.

For a new incident X and the analysed corpus (campaigns from `nexus.cluster`):
1. rarity is recomputed with X included (X's own values never make them look common: the pair is excluded);
2. candidates come from the retrieval union restricted to pairs touching X;
3. each candidate pair is scored by `synapse.score_pair`; per campaign: best pair, support (members with a
   same-campaign pair), campaign-level bits of the best pair;
4. decision:
   - ASSIGN when the best campaign has a same-campaign pair and no rival campaign is within `margin` bits;
   - ABSTAIN when two campaigns are supported and too close, or when campaign-level evidence is in the grey
     band (`abstain_min` ≤ bits < `link`) without contradictions AND comes from operator-tier genes or from two
     families (one capped family alone, e.g. shared OCR text, is a new pattern); kit/platform bits never assign;
   - a contradiction (different scheme, no shared rare campaign gene) is evidence against: new pattern;
   - NEW_CAMPAIGN otherwise; `group_id` is set when kit-level evidence still ties X to a known group.
`AssignmentCandidate.probability` is the pair's uncalibrated logistic of bits unless the LR path is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hydra.contracts import Assignment, AssignmentCandidate, AssignmentDecision, Campaign, CampaignGroup, Hypothesis
from hydra.entropy import Rarity
from hydra.labels_pl import num_pl
from hydra.retrieval import candidates as retrieve
from hydra.retrieval import query_scope
from hydra.synapse import FAMILY_PL, IncidentGenome, PairEval, Thresholds, score_pair

MODEL = "assign_open_set@1"
MARGIN, MAX_CANDIDATES, ABSTAIN_MIN = 3.0, 10, 6.0  # decision defaults (see `assign`)


@dataclass
class AssignResult:
    assignment: Assignment
    pairs: dict[tuple[str, str], PairEval]
    conformal: dict | None = None  # prediction set, p-values, calibration size (stored in assignments.detail)


def assign(new: IncidentGenome, genomes: dict[str, IncidentGenome], campaigns: list[Campaign],
           groups: list[CampaignGroup], rarity: Rarity | None = None, th: Thresholds | None = None,
           margin: float = MARGIN, max_candidates: int = MAX_CANDIDATES, abstain_min: float = ABSTAIN_MIN,
           conformal: dict | None = None, progress: Any | None = None, fusion: Any | None = None,
           cannot_link: list[tuple[str, str]] | None = None,
           cand: dict[tuple[str, str], set[str]] | None = None) -> AssignResult:
    """`abstain_min`: campaign-level bits (kit/platform excluded, contradictions subtracted) from which a
    below-threshold best candidate goes to the analyst instead of becoming a new pattern.
    `conformal`: calibration from `hydra.conformal.calibrate`; adds a prediction set (and decides by it only when
    the calibration is large enough, see `conformal.MIN_CALIBRATION`).
    `cannot_link`: active analyst cannot-link pairs of the base revision; a new incident with link-strength
    evidence to two campaigns that an analyst separated is a bridge across that decision and abstains.
    `cand`: precomputed candidates of `new` (`retrieval.candidates_per_query`), identical to the query retrieval."""
    th = th or Thresholds()
    group_of = {c.id: c.group_id for c in campaigns}
    rarity = assignment_rarity(new, genomes, campaigns, rarity)
    info: dict[str, Any] = {}
    pairs, ranked = campaign_supports(new, genomes, campaigns, rarity, th, progress=progress, info=info,
                                      fusion=fusion, cand=cand)
    res = _decide(new, ranked, pairs, group_of, th, margin, max_candidates, abstain_min)
    if conformal is not None:
        from hydra.conformal import apply_conformal

        apply_conformal(res, ranked, conformal, group_of, th)
    if cannot_link:
        bridge_abstain(res, ranked, campaigns, cannot_link, th)
    res.assignment.meta.update(explain(new, genomes, ranked, rarity, th, info, res.conformal))
    if progress:
        progress.stage("decision", decision=res.assignment.decision, campaign_id=res.assignment.campaign_id,
                       group_id=res.assignment.group_id, pairs_scored=len(pairs))
    return res


BRIDGE_REASON = "bridges cannot-link"  # machine code, kept in meta.bridge_cannot_link.reason only
BRIDGE_REASON_PL = "łączy kampanie rozdzielone decyzją analityka"  # CR-UI-FINAL-1: the Polish text for reason_pl


def separated_campaigns(campaigns: list[Campaign], cannot_link: list[tuple[str, str]]
                        ) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Campaign pairs (sorted ids) separated by at least one analyst cannot-link → the cannot-link pairs."""
    member_of = {m: c.id for c in campaigns for m in c.members}
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for a, b in cannot_link:
        ca, cb = member_of.get(a), member_of.get(b)
        if ca and cb and ca != cb:
            out.setdefault((ca, cb) if ca < cb else (cb, ca), []).append((a, b) if a < b else (b, a))
    return out


def bridge_abstain(res: AssignResult, ranked: Ranked, campaigns: list[Campaign],
                   cannot_link: list[tuple[str, str]], th: Thresholds) -> bool:
    """ADOPT #14: link-strength evidence to two campaigns separated by an analyst cannot-link → ABSTAIN.
    Assigning would silently re-join what the analyst split; the decision goes back to the analyst with both
    sides of the evidence (`meta.bridge_cannot_link`, shown as a three-column card)."""
    sep = separated_campaigns(campaigns, cannot_link)
    strong = [(s, cid, pe) for s, _k, _sup, cid, pe in ranked
              if s >= th.link and pe.score.hypothesis == Hypothesis.SAME_CAMPAIGN]
    for i, (s1, c1, pe1) in enumerate(strong):
        for s2, c2, pe2 in strong[i + 1:]:
            key = (c1, c2) if c1 < c2 else (c2, c1)
            if key not in sep:
                continue
            a = res.assignment
            a.meta["bridge_cannot_link"] = {
                "reason": BRIDGE_REASON, "campaigns": [c1, c2], "bits": [round(s1, 3), round(s2, 3)],
                "cannot_link": [list(p) for p in sep[key]], "pairs": [[pe1.score.a, pe1.score.b],
                                                                       [pe2.score.a, pe2.score.b]],
                "previous_decision": a.decision, "previous_campaign": a.campaign_id}
            a.decision, a.campaign_id, a.group_id = AssignmentDecision.ABSTAIN, None, None
            a.reason_pl = (f"Wstrzymano: materiał łączy kampanie {c1} i {c2} ({num_pl(s1)} i {num_pl(s2)} bit"
                           " dowodów), które"
                           f" analityk rozdzielił decyzją „nie może być połączone” ({BRIDGE_REASON_PL}). Przypisanie"
                           " połączyłoby je ponownie; wymaga decyzji analityka.")
            return True
    return False


def _level_split(pe: PairEval) -> dict[str, Any]:
    s = pe.score
    return {"campaign_bits": pe.campaign_bits, "kit_bits": pe.kit_bits, "asset_bits": pe.asset_bits,
            "support": pe.support, "operator_bits": pe.operator_bits,
            "campaign_by_family": pe.campaign_by_family, "family_bits": s.family_bits,
            "hypothesis": s.hypothesis, "pair": [s.a, s.b], "contradictions": len(s.contradictions)}


def explain(new: IncidentGenome, genomes: dict[str, IncidentGenome], ranked: Ranked, rarity: Rarity,
            th: Thresholds, info: dict[str, Any], conformal: dict | None) -> dict[str, Any]:
    """Assignment.meta: top-3 candidates split by level and family, margin #1 − #2, bits missing to the link
    threshold (and whether the operator/two-family rule is met), robustness of the best link, candidates per
    index, conformal set. One extra rescoring per family for the best pair only (small overhead)."""
    from hydra.synapse import OPERATOR_STRONG_BITS, STRONG_FAMILY_BITS, robustness

    top = [{"campaign_id": cid, "supporting_members": sup, **_level_split(pe)} for _s, _k, sup, cid, pe in ranked[:3]]
    s1 = ranked[0][0] if ranked else 0.0
    s2 = ranked[1][0] if len(ranked) > 1 else 0.0
    out: dict[str, Any] = {"top_candidates": top, "margin_bits": round(s1 - s2, 3),
                           "missing_bits_to_link": round(max(0.0, th.link - s1), 3), "link_threshold": th.link,
                           "candidates": info.get("candidates"), "retrieval_scope": info.get("retrieval_scope")}
    if ranked:
        best = ranked[0][4]
        top[0].update(best_pair_caveats(best, {new.incident_id: new, **genomes}))
        strong = sum(1 for v in best.campaign_by_family.values() if v >= STRONG_FAMILY_BITS)
        out["link_rule_met"] = bool(best.operator_bits >= OPERATOR_STRONG_BITS or strong >= 2)
        other = best.score.b if best.score.a == new.incident_id else best.score.a
        if other in genomes:
            out["best_link_robustness"] = robustness(new, genomes[other], rarity, best, th)
            best.robustness = out["best_link_robustness"]  # stored with the assignment pair (pairs.detail)
    if conformal is not None:
        out["conformal"] = conformal
    return out


def _specificity(e: Any) -> int:
    """How precisely a pointer locates the evidence: a frame/time (2), a region or text span (1 each), a HAR entry or
    DOM path (1). Timecode 0 without a bbox is the whole-file encoding of CR-UI-FINAL-3 and counts 0."""
    if e is None:
        return -1
    tc = e.timecode_s
    return ((2 if tc is not None and tc > 0 else 0) + (1 if e.bbox is not None and tuple(e.bbox[:2]) != (0, 0) else 0)
            + (1 if e.char_range is not None else 0) + (1 if e.har_entry is not None or e.dom_path else 0))


def _pointer(g: IncidentGenome | None, kind: str, value: str | None, given: Any) -> tuple[Any, bool]:
    """The most specific pointer of the same (kind, value) gene in that incident; (pointer, replaced)."""
    best, score = given, _specificity(given)
    for gene in (g.discrete.get((kind, value), []) if g is not None and value is not None else []):
        s = _specificity(gene.evidence)
        if s > score:
            best, score = gene.evidence, s
    return best, best is not given


def best_pair_caveats(pe: PairEval, genomes: dict[str, IncidentGenome], n: int = 5) -> dict[str, Any]:
    """CR-UI-FINAL-4: contradictions and missing families of the best pair on `top_candidates[0]`, plus evidence
    pointers of its strongest reasons, frame/region-level where the incident has one for the same value (display
    only; a whole-file pointer is kept when nothing more precise exists)."""
    s = pe.score
    ev = []
    for r in [r for r in s.reasons if r.bits > 0][:n]:
        pa, ra = _pointer(genomes.get(s.a), r.kind, r.value_a, r.evidence_a)
        pb, rb = _pointer(genomes.get(s.b), r.kind, r.value_b if r.value_b is not None else r.value_a, r.evidence_b)
        ev.append({"family": r.family, "kind": r.kind, "value": r.value_a, "bits": r.bits,
                   "evidence_a": pa.model_dump(mode="json") if pa is not None else None,
                   "evidence_b": pb.model_dump(mode="json") if pb is not None else None,
                   "refined": bool(ra or rb)})
    return {"contradiction_list": [{"kind": c.kind, "family": c.family, "bits": c.bits,
                                    "explanation_pl": c.explanation_pl} for c in s.contradictions],
            "missing_families": list(s.missing_families), "evidence": ev}


def assignment_rarity(new: IncidentGenome, genomes: dict[str, IncidentGenome], campaigns: list[Campaign],
                      rarity: Rarity | None) -> Rarity:
    """The one rarity regime for open-set assignment, whatever the caller passes: campaign-level df counts
    incidents (as in batch `analyse`), kit-level df counts campaign groups (`kit_unit_of`). A rarity built with
    campaign/group units for everything (EVAL harness, 2026-11-21) made umbrella values look unique and sent
    held-out campaigns to their sibling; such a rarity is rebuilt here."""
    counted = getattr(rarity, "member_ids", None) or (rarity._all_ids() if rarity is not None else set())
    if rarity is None or rarity.unit_of or new.incident_id not in counted and new.genes:
        from hydra.pipeline import rarity_for

        rarity = rarity_for({**genomes, new.incident_id: new})
    if rarity.kit_unit_of is None:
        rarity.kit_unit_of = {m: (c.group_id or c.id) for c in campaigns for m in c.members}
    if rarity.operator_unit_of is None:  # review M1: operator-tier df counts known campaigns, not their members
        rarity.operator_unit_of = {m: c.id for c in campaigns for m in c.members}
    return rarity


Ranked = list[tuple[float, float, int, str, PairEval]]  # (support, kit bits, supporting members, campaign, pair)


def candidate_index_counts(cand: dict[tuple[str, str], set[str]]) -> dict[str, Any]:
    """Candidates per index: {"exact": {kind: n}, "near": {...}, "knn": {...}, "all_pairs": n, "total": n}."""
    out: dict[str, Any] = {"total": len(cand), "all_pairs": 0, "exact": {}, "near": {}, "knn": {}}
    for sources in cand.values():
        for src in sources:
            if src == "all_pairs":
                out["all_pairs"] += 1
                continue
            idx, _, kind = src.partition(":")
            bucket = out.setdefault(idx, {})
            bucket[kind] = bucket.get(kind, 0) + 1
    return out


def campaign_supports(new: IncidentGenome, genomes: dict[str, IncidentGenome], campaigns: list[Campaign],
                      rarity: Rarity, th: Thresholds, progress: Any | None = None, info: dict | None = None,
                      fusion: Any | None = None, cand: dict[tuple[str, str], set[str]] | None = None
                      ) -> tuple[dict[tuple[str, str], PairEval], Ranked]:
    """Score `new` against candidate members; per campaign the best pair by `support` (the batch number)."""
    membership = {m: c.id for c in campaigns for m in c.members}
    universe = dict(genomes)
    universe[new.incident_id] = new
    if progress:
        progress.stage("candidates")
    if cand is None:
        cand = retrieve(universe, rarity, queries=[new.incident_id])
    counts = candidate_index_counts(cand)
    if info is not None:
        info["candidates"] = counts
        info["retrieval_scope"] = query_scope(universe, rarity, new.incident_id)  # U03 telemetry, no scoring effect
    if progress:
        progress.stage("scoring", candidates=counts, pairs_total=len(cand), pairs_scored=0)
    pairs: dict[tuple[str, str], PairEval] = {}
    per_campaign: dict[str, list[PairEval]] = {}
    for (a, b), sources in cand.items():
        other = b if a == new.incident_id else a
        if other not in membership:
            continue
        pe = score_pair(new, genomes[other], rarity, th, sorted(sources))
        if fusion is not None and getattr(fusion, "_lr", None) is not None:
            fusion.apply(pe, th)  # the batch run's fusion model (audit §7.3)
        pairs[(a, b)] = pe
        per_campaign.setdefault(membership[other], []).append(pe)
        if progress:
            progress.set(pairs_scored=len(pairs))
    ranked: Ranked = []
    for cid, pes in per_campaign.items():
        best = max(pes, key=lambda p: (p.support, p.score.total_bits))
        support = sum(1 for p in pes if p.score.hypothesis == Hypothesis.SAME_CAMPAIGN)
        # kit evidence of the campaign = the strongest kit pair, not the kit of the best-support pair
        ranked.append((best.support, max(p.kit_bits for p in pes), support, cid, best))
    ranked.sort(key=lambda t: (-t[0], -t[2], -t[1], t[3]))
    return pairs, ranked


def _decide(new: IncidentGenome, ranked: Ranked, pairs: dict[tuple[str, str], PairEval],
            group_of: dict[str, str | None], th: Thresholds, margin: float, max_candidates: int,
            abstain_min: float) -> AssignResult:
    cands = [AssignmentCandidate(campaign_id=cid, probability=best.score.probability, best_pair=best.score,
                                 support=support) for _s, _k, support, cid, best in ranked[:max_candidates]]

    decision, cid, gid = AssignmentDecision.NEW_CAMPAIGN, None, None
    if not ranked:
        reason = "Brak kandydatów: żaden znany incydent nie dzieli z nowym materiałem rzadkiej cechy."
        return AssignResult(Assignment(incident_id=new.incident_id, decision=decision, candidates=[],
                                       reason_pl=reason, model=MODEL), pairs)
    s1, k1, sup1, c1, best1 = ranked[0]
    s2 = ranked[1][0] if len(ranked) > 1 else 0.0
    top_reasons = ", ".join(r.explanation_pl.split(" — ")[0] for r in best1.score.reasons[:3]) or "brak"
    if best1.score.hypothesis == Hypothesis.SAME_CAMPAIGN and s1 >= th.link:
        if len(ranked) > 1 and s2 >= th.link and s1 - s2 < margin:
            decision = AssignmentDecision.ABSTAIN
            reason = (f"Niejednoznaczne: dwie kampanie mają silne dowody ({num_pl(s1)} vs {num_pl(s2)} bit, różnica"
                      f" < {num_pl(margin, '.0f')} bit). Wymaga decyzji analityka.")
        else:
            decision, cid = AssignmentDecision.ASSIGN, c1
            gid = group_of.get(c1)
            reason = (f"Przypisano do kampanii {c1}: {num_pl(s1)} bit dowodów na poziomie kampanii, wsparcie"
                      f" {sup1} incydentów; najmocniejsze powody: {top_reasons}.")
    elif (best1.campaign_bits >= abstain_min and best1.score.hypothesis == Hypothesis.ABSTAIN
          and not best1.score.contradictions
          and (best1.operator_bits > 0 or sum(1 for v in best1.campaign_by_family.values() if v > 0) >= 2)):
        decision = AssignmentDecision.ABSTAIN
        reason = (f"Niejednoznaczne dowody wobec kampanii {c1} ({num_pl(best1.campaign_bits)} bit na poziomie kampanii,"
                  f" brak dowodu operatora ani dwóch silnych rodzin cech); najmocniejsze powody:"
                  f" {top_reasons}. Do weryfikacji przez analityka.")
    else:
        kit_best = max(ranked, key=lambda t: t[1])
        if kit_best[1] >= th.kit and group_of.get(kit_best[3]):
            gid = group_of[kit_best[3]]
        elif kit_best[1] >= th.kit:
            gid = None
        if s1 >= th.link:  # CR-AQ-1 (iii) / review item 7: enough bits, but the family rule blocks the link
            fl = sorted(f for f, v in best1.campaign_by_family.items() if v > 0)
            fpl = [FAMILY_PL.get(f, f) for f in fl]  # CR-UI-FINAL-2: Polish family names, not codes
            src = (f"tylko z jednej rodziny cech ({fpl[0]})" if len(fl) == 1 else
                   f"z rodzin cech {', '.join(fpl) or 'brak'}, ale mniej niż dwie mają co najmniej 8 bit")
            reason = (f"Nowy wzorzec: najlepsza kampania {c1} ma {num_pl(s1)} bit dowodów na poziomie kampanii (próg"
                      f" {num_pl(th.link, '.0f')}), ale {src} i bez silnego dowodu operatora (domena, identyfikator"
                      f" śledzący,"
                      f" konto: {num_pl(best1.operator_bits)} z wymaganych 8 bit).")
        else:
            reason = (f"Nowy wzorzec: najlepsza kampania {c1} ma tylko {num_pl(max(s1, 0.0))} bit dowodów na poziomie"
                      f" kampanii (próg {num_pl(th.link, '.0f')}).")
        if best1.score.contradictions:
            reason += f" {best1.score.contradictions[0].explanation_pl}."
        if kit_best[1] >= th.kit:
            reason += (f" Wspólny zestaw narzędzi (kit) z kampanią {kit_best[3]}: {num_pl(kit_best[1])} bit"
                       + (f" — pasuje do grupy {gid}." if gid else "."))
    return AssignResult(Assignment(incident_id=new.incident_id, decision=decision, campaign_id=cid, group_id=gid,
                                   candidates=cands, reason_pl=reason, model=MODEL), pairs)


def analyse_input(kind: str, payload, settings, **kwargs):
    """Entry point for UI `POST /v1/analyse` (url | screenshot | hint | file); see `hydra.pipeline.analyse_input`."""
    from hydra.pipeline import analyse_input as _impl

    return _impl(kind, payload, settings, **kwargs)
