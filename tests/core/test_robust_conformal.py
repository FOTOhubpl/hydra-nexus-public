"""Leave-one-family-out robustness and split-conformal assignment (synthetic, no organiser data)."""

from __future__ import annotations

import json

import numpy as np

from hydra.config import Settings
from hydra.conformal import MIN_CALIBRATION, apply_conformal, p_value
from hydra.contracts import Family, Gene
from hydra.db import connect
from hydra.entropy import build_rarity
from hydra.pipeline import analyse, pair_score_from_detail
from hydra.synapse import Thresholds, build_genome, robustness, score_pair
from tests.core.test_pipeline import corpus, test_extractors  # noqa: F401 — fixture


def g(iid, kind, value, group, fam=Family.INF):
    return Gene(incident_id=iid, family=fam, kind=kind, value=value, extractor="t@1", dependency_group=group)


def _pair(extra_a, extra_b, n=12):
    genes = {f"i{k}": [g(f"i{k}", "host", f"h{k}.example", "INF:operator:host")] for k in range(n)}
    genes["i0"] += extra_a
    genes["i1"] += extra_b
    r = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    return gen, r


def test_single_family_link_is_not_robust_and_multi_family_is():
    op = lambda i: [g(i, "operator_id", "acct-1", "INF:operator:acct"), g(i, "tracker_id", "px-9", "INF:operator:tr")]  # noqa: E731
    gen, r = _pair(op("i0"), op("i1"))
    pe = score_pair(gen["i0"], gen["i1"], r)
    rb = robustness(gen["i0"], gen["i1"], r, pe)
    assert rb["link"] == "campaign" and not rb["robust_link"] and rb["critical_families"] == ["INF"]
    txt = lambda i: [g(i, "phrase", f"p{k}", f"TXT:phrase:p{k}", Family.TXT) for k in range(3)]  # noqa: E731
    vis = lambda i: [g(i, "artifact_sha256", "f" * 64, "core:vis_files", Family.VIS)]  # noqa: E731
    gen, r = _pair(op("i0") + txt("i0") + vis("i0"), op("i1") + txt("i1") + vis("i1"))
    pe = score_pair(gen["i0"], gen["i1"], r)
    rb = robustness(gen["i0"], gen["i1"], r, pe)
    assert rb["robust_link"], rb
    assert set(rb["families"]) == {"INF", "TXT", "VIS"} and all(v["still_linked"] for v in rb["families"].values())


def test_robustness_is_stored_in_pair_detail(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with connect(settings.db_path, readonly=True) as conn:
        d = json.loads(conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a='nask-1' AND b='nask-2'",
                                    (res.run_id,)).fetchone()[0])
        unlinked = json.loads(conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a='nask-1' AND b='nask-6'",
                                           (res.run_id,)).fetchone()[0])
    assert d["robustness"]["link"] == "campaign" and isinstance(d["robust_link"], bool)
    assert "robustness" not in unlinked
    assert pair_score_from_detail(d).hypothesis == "same_campaign"  # extra keys stripped for validation
    assert res.metrics["robustness"]["campaign_links"] >= 1
    cal = res.metrics["conformal"]
    assert cal["n"] == 3 + 2 and cal["usable"] is False and cal["held_out_n"] == 5  # A (3) + B (2) members


def test_p_values_and_prediction_sets():
    a_cal = np.array([-40.0, -35.0, -30.0] + [-25.0] * 30)
    assert p_value(-50.0, a_cal) == 1.0
    assert p_value(0.0, a_cal) == 1 / 34

    class R:  # minimal AssignResult stand-in
        def __init__(self):
            from hydra.contracts import Assignment

            self.assignment = Assignment(incident_id="x", decision="abstain", reason_pl="r", model="m")
            self.conformal = None

    from types import SimpleNamespace

    def pe(support, op=24.0):
        return SimpleNamespace(score=SimpleNamespace(contradictions=[]), support=support, operator_bits=op,
                               campaign_by_family={"INF": support})

    cal = {"alphas": list(a_cal), "eps": 0.1, "usable": len(a_cal) >= MIN_CALIBRATION}
    # Review M2 (SCORING-HARDEN, 2026-11-21): the calibrator never promotes. A one-campaign set confirms a threshold
    # ASSIGN to that campaign; it no longer turns a threshold ABSTAIN into an assignment.
    for start, ranked, want, size, by in (
            ("assign", [(30.0, 0, 1, "C1", pe(30)), (1.0, 0, 0, "C2", pe(1))], "assign", 1, "conformal"),
            ("abstain", [(30.0, 0, 1, "C1", pe(30)), (1.0, 0, 0, "C2", pe(1))], "abstain", 1, "threshold"),
            ("assign", [(30.0, 0, 1, "C1", pe(30)), (28.0, 0, 1, "C2", pe(28))], "abstain", 2, "conformal"),
            ("assign", [(2.0, 0, 0, "C1", pe(2))], "new_campaign", 0, "conformal")):
        res = R()
        if start == "assign":
            res.assignment.decision, res.assignment.campaign_id = "assign", "C1"
        apply_conformal(res, ranked, cal, {"C1": "G1", "C2": None}, Thresholds())
        assert res.assignment.decision == want and res.conformal["set_size"] == size
        assert res.conformal["decided_by"] == by
    small = {"alphas": [-40.0, -30.0], "eps": 0.1, "usable": False}
    res = R()
    apply_conformal(res, [(12.0, 0, 0, "C1", pe(12))], small, {}, Thresholds())
    assert res.assignment.decision == "abstain" and res.conformal["decided_by"] == "threshold"
    assert res.conformal["prediction_set"] == ["C1"]  # n=2: nothing can be excluded, honestly reported


def test_conformal_never_assigns_without_evidence_audit_a12():
    """Audit reproduction `conformal_overrides_no_evidence_decision`: empty genomes, one campaign, 20 zero alphas."""
    from hydra.assign import assign
    from hydra.contracts import Campaign
    from hydra.synapse import build_genome

    unknown, known = build_genome("z-unknown", [], {}), build_genome("a-known", [], {})
    campaigns = [Campaign(id="C-one", label="Synthetic", members=["a-known"])]
    ordinary = assign(unknown, {"a-known": known}, campaigns, [])
    conf = assign(unknown, {"a-known": known}, campaigns, [], conformal={"alphas": [0.0] * 20, "eps": 0.1,
                                                                         "usable": True})
    assert ordinary.assignment.decision == conf.assignment.decision == "new_campaign"
    assert conf.conformal is None or conf.conformal["prediction_set"] == []
    assert "Przypisano" not in conf.assignment.reason_pl


def test_held_out_campaign_returns_new_with_group_even_with_group_unit_rarity(settings: Settings, make_png,
                                                                              tmp_path, test_extractors):  # noqa: F811
    from hydra.assign import assign
    from hydra.pipeline import rarity_for

    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    camps = res.clustering.campaigns
    held = next(c for c in camps if "nask-1" in c.members)
    rest = [c for c in camps if c.id != held.id]
    gen = {k: v for k, v in res.genomes.items() if k not in held.members}
    unit_of = {m: (c.group_id or c.id) for c in rest for m in c.members}  # the EVAL harness' regime
    a = assign(res.genomes["nask-1"], gen, rest, res.clustering.groups,
               rarity_for({**gen, "nask-1": res.genomes["nask-1"]}, unit_of)).assignment
    assert a.decision == "new_campaign" and a.group_id == held.group_id is not None, a.reason_pl
