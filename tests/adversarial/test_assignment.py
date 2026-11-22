"""Open-set decisions and adversarial conformal metadata through the public assign API."""

import pytest

from hydra.assign import assign
from hydra.contracts import Campaign
from hydra.synapse import build_genome
from tests.adversarial.helpers import corpus, gene


def test_completely_unrelated_material_stays_new_campaign():
    rows = {iid: [gene(iid, "tracker_id", f"pixel-{iid}", "INF:operator:tracker")] for iid in ("a", "b", "new")}
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    campaigns = [Campaign(id=f"C-{iid}", label="Synthetic", members=[iid]) for iid in ("a", "b")]
    result = assign(new, genomes, campaigns, []).assignment
    assert result.decision == "new_campaign"
    assert result.campaign_id is None
    assert result.reason_pl.startswith("Nowy wzorzec:")


def test_equal_evidence_for_two_campaigns_abstains_in_polish():
    rows = {
        "a": [gene("a", "tracker_id", "pixel-a", "INF:operator:tracker")],
        "b": [gene("b", "tracker_id", "pixel-b", "INF:operator:tracker")],
        "new": [gene("new", "tracker_id", f"pixel-{c}", "INF:operator:tracker") for c in ("a", "b")],
    }
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    campaigns = [Campaign(id=f"C-{iid}", label="Synthetic", members=[iid]) for iid in ("a", "b")]
    result = assign(new, genomes, campaigns, []).assignment
    assert result.decision == "abstain" and result.campaign_id is None
    assert "Niejednoznaczne" in result.reason_pl and "analityka" in result.reason_pl


@pytest.mark.parametrize("calibration", [None, {"alphas": [0.0] * 20, "eps": 0.1, "usable": True}])
def test_empty_incident_never_acquires_a_campaign_from_calibration(calibration):
    known = build_genome("known", [], {})
    new = build_genome("new", [], {})
    result = assign(new, {"known": known}, [Campaign(id="C", label="Synthetic", members=["known"])], [],
                    conformal=calibration)
    assert result.assignment.decision == "new_campaign"
    assert result.assignment.campaign_id is None
    assert result.conformal is None or result.conformal["prediction_set"] == []


def test_rotated_domain_with_same_rare_tracker_assigns_to_campaign():
    rows = {iid: [
        gene(iid, "host", f"{iid}.invalid", "INF:operator:host"),
        gene(iid, "tracker_id", "synthetic-operator-7f829a", "INF:operator:tracker"),
    ] for iid in ("known", "new")}
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    result = assign(new, genomes, [Campaign(id="C", label="Synthetic", members=["known"])], []).assignment
    assert result.decision == "assign" and result.campaign_id == "C"
    assert result.candidates[0].best_pair.calibrated is False
    assert "bit" in result.reason_pl


@pytest.mark.parametrize("alphas", [[], [-100.0], [-100.0] * 19], ids=["empty", "one", "nineteen"])
def test_undersized_calibration_cannot_override_threshold_when_usable_flag_is_wrong(alphas):
    rows = {iid: [gene(iid, "tracker_id", "synthetic-rare-pixel", "INF:operator:tracker")] for iid in ("a", "new")}
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    campaigns = [Campaign(id="C", label="Synthetic", members=["a"])]
    ordinary = assign(new, genomes, campaigns, [])
    assert ordinary.assignment.decision == "assign"
    result = assign(new, genomes, campaigns, [],
                    conformal={"alphas": alphas, "eps": 0.1, "usable": True})
    assert result.conformal["decided_by"] == "threshold"
    assert result.assignment.decision == ordinary.assignment.decision
