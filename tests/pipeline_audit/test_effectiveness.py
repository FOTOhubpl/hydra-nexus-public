"""Independent synthetic challenges to the real method; no extraction/model claims.

Labels below are fixed by the generator, never obtained from predicted clusters.
Inputs are public Gene/IncidentGenome contracts. No images, model weights, external
corpus or organiser material is loaded. Strict xfails name root-owned contract requests.
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections import Counter
from itertools import combinations

import numpy as np
import pytest

from eval.gate import compare
from hydra.assign import assign
from hydra.conformal import calibrate
from hydra.contracts import EvidencePointer, Gene
from hydra.genome.webparse import OPID_PLATFORM, OPID_RX, dependency_group
from hydra.pipeline import rarity_for, score_and_cluster, self_assign
from hydra.retrieval import candidates
from hydra.synapse import Thresholds, build_genome, score_pair

TH = Thresholds()
# Distinct groups declared by production TIERS / har._emit_scan, not invented
# independence for correlated landing-domain or tracker-token observations.
# All values are fabricated contract inputs; the public-looking IP is never contacted.
IDENTIFIERS = (
    ("operator_id", "landing_cdn_account:audit-account-7f2c"),
    ("tracker_id", "fb_pixel:782346591082735"),
    ("landing_host", "landing.audit-campaign.test"),
    ("lead_endpoint", "capture.audit-leads.test/api/submit"),
    ("linked_host", "assets.audit-media.test"),
    ("server_ip", "11.23.45.67"),
)


def gene(iid, kind, value, group, family="INF", **kwargs):
    return Gene(incident_id=iid, family=family, kind=kind, value=value, dependency_group=group,
                extractor="pipeline_audit_synthetic@1", evidence=EvidencePointer(note="generated audit fixture"),
                **kwargs)


def genomes(rows):
    return {iid: build_genome(iid, genes, {}) for iid, genes in rows.items()}


def identity(iid, campaign):
    return [gene(iid, "tracker_id", f"synthetic-pixel-{campaign}", "INF:operator:tracker"),
            gene(iid, "entity_phone", f"synthetic-phone-{campaign}", "TXT:operator:contact", "TXT")]


def background(iid):
    return [gene(iid, "private_identity", f"private-{iid}", "INF:operator:private"),
            gene(iid, "asn", "13335", "INF:boilerplate:cdn_asn"),
            gene(iid, "host", "cdnjs.cloudflare.com", "INF:boilerplate:cdn"),
            gene(iid, "tracker_id", "YOUR_PIXEL_ID", "INF:boilerplate:default"),
            gene(iid, "css_build_hash", "shared-kit", "INF:kit:css"),
            gene(iid, "phrase", "synthetic generic cookie banner", "TXT:boilerplate:cookie", "TXT")]


def labelled_population():
    rows, truth = {}, {}
    for campaign in range(6):
        for member in range(2):
            iid = f"known-{campaign}-{member}"
            rows[iid] = identity(iid, campaign) + background(iid)
            truth[iid] = f"truth-{campaign}"
    for i in range(12):
        iid = f"unrelated-{i}"
        rows[iid], truth[iid] = background(iid), f"singleton-{i}"
    return genomes(rows), truth


def pair_confusion(predicted, truth):
    out = Counter({"tp": 0, "tn": 0, "fp": 0, "fn": 0})
    for a, b in combinations(sorted(truth), 2):
        same_truth, same_pred = truth[a] == truth[b], predicted[a] == predicted[b]
        out["tp" if same_truth and same_pred else "fn" if same_truth else "fp" if same_pred else "tn"] += 1
    return dict(out)


def test_labelled_gene_population_pairwise_confusion():
    population, truth = labelled_population()
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    assert pair_confusion(clustering.membership, truth) == {"tp": 6, "tn": 270, "fp": 0, "fn": 0}


@pytest.mark.parametrize("campaign", range(6))
def test_known_variant_assigns_by_generated_truth(campaign):
    population, truth = labelled_population()
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    iid = f"new-variant-{campaign}"
    query = build_genome(iid, identity(iid, campaign) + background(iid), {})
    outcome = assign(query, population, clustering.campaigns, clustering.groups).assignment
    assert outcome.decision == "assign"
    assigned = next(c for c in clustering.campaigns if c.id == outcome.campaign_id)
    assert {truth[m] for m in assigned.members} == {f"truth-{campaign}"}


@pytest.mark.parametrize("campaign", range(6))
def test_second_independent_infrastructure_identity_recovers_assignment(campaign):
    population, truth = labelled_population()
    rows = {iid: list(g.genes) for iid, g in population.items()}
    for iid, label in truth.items():
        if label.startswith("truth-"):
            rows[iid].append(gene(iid, "landing_host", f"{label}.example.test", "INF:operator:landing"))
    population = genomes(rows)
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    iid = f"strong-variant-{campaign}"
    query = build_genome(iid, identity(iid, campaign) + background(iid) + [
        gene(iid, "landing_host", f"truth-{campaign}.example.test", "INF:operator:landing")], {})
    result = assign(query, population, clustering.campaigns, clustering.groups).assignment
    assert result.decision == "assign"
    assert result.campaign_id == clustering.membership[f"known-{campaign}-0"]


@pytest.mark.parametrize("campaign", range(6))
def test_unknown_operator_with_shared_kit_is_new(campaign):
    population, _truth = labelled_population()
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    iid = f"new-operator-{campaign}"
    query = build_genome(iid, identity(iid, f"unknown-{campaign}") + background(iid), {})
    outcome = assign(query, population, clustering.campaigns, clustering.groups).assignment
    assert outcome.decision == "new_campaign"
    assert outcome.campaign_id is None
    assert outcome.reason_pl


def test_equal_operator_evidence_to_two_campaigns_abstains():
    population, _truth = labelled_population()
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    query = build_genome("ambiguous", identity("ambiguous", 0) + identity("ambiguous", 1), {})
    result = assign(query, population, clustering.campaigns, clustering.groups).assignment
    assert result.decision == "abstain" and result.campaign_id is None
    assert "Niejednoznaczne" in result.reason_pl


def test_missing_observation_does_not_change_pair_support():
    rows = {iid: identity(iid, 0) for iid in ("a", "b")}
    original = genomes(rows)
    before = score_pair(original["a"], original["b"], rarity_for(original))
    for iid in rows:
        rows[iid].append(gene(iid, "missing_photo", None, "VIS:asset:photo", "VIS", status="unavailable"))
    extended = genomes(rows)
    after = score_pair(extended["a"], extended["b"], rarity_for(extended))
    assert before.support == after.support
    assert "VIS" in after.score.missing_families


def growth_population(n):
    rows = {f"i{i:02d}": [gene(f"i{i:02d}", "private_identity", f"unique-{i}", "INF:operator:private")]
            for i in range(n)}
    for i in range(13):
        iid = f"i{i:02d}"
        rows[iid] += growth_identity(iid)
    return genomes(rows)


def growth_identity(iid):
    """Model-free public genes with actual extractor groups, not raw HAR extraction."""
    result = []
    for kind, value in IDENTIFIERS:
        if kind == "operator_id":
            subtype = value.split(":", 1)[0]
            if subtype not in OPID_RX:
                raise ValueError(f"unsupported production operator subtype: {subtype}")
            # har._emit_scan derives this dynamic subgroup from the operator subtype.
            tier = "platform" if subtype in OPID_PLATFORM else "operator"
            group = dependency_group(kind, tier=tier, sub=subtype)
        else:
            group = dependency_group(kind)
        result.append(gene(iid, kind, value, group, quality=0.6 if kind == "linked_host" else 1.0))
    return result


def test_growth_fixture_has_six_distinct_production_operator_groups():
    shared = growth_identity("validation")
    assert len({g.dependency_group for g in shared}) == 6
    assert all(g.dependency_group.startswith("inf:operator:") for g in shared)
    assert ipaddress.ip_address(dict(IDENTIFIERS)["server_ip"]).is_global


def test_small_corpus_growth_control():
    population = growth_population(60)
    rarity = rarity_for(population)
    assert score_pair(population["i00"], population["i01"], rarity).score.hypothesis == "same_campaign"
    assert ("i00", "i01") in candidates(population, rarity)


def test_unrelated_61st_incident_preserves_supported_candidate():
    population = growth_population(61)
    rarity = rarity_for(population)
    assert score_pair(population["i00"], population["i01"], rarity).score.hypothesis == "same_campaign"
    assert ("i00", "i01") in candidates(population, rarity)


def test_unrelated_61st_incident_preserves_campaign_connectivity():
    population = growth_population(61)
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    assert len({clustering.membership[f"i{i:02d}"] for i in range(13)}) == 1


def test_new_variant_of_large_campaign_remains_assignable():
    population = growth_population(60)
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    query = build_genome("variant", growth_identity("variant"), {})
    combined = {**population, "variant": query}
    assert score_pair(query, population["i00"], rarity_for(combined)).score.hypothesis == "same_campaign"
    result = assign(query, population, clustering.campaigns, clustering.groups).assignment
    assert result.decision == "assign"
    assert result.campaign_id == clustering.membership["i00"]


BASE_GATE = {"backend": "hydra-core", "crashes": 0,
             "sample": {"loo_accuracy": 1.0, "open_set_rejection": 1.0, "false_merge_pairs": 0}}


@pytest.mark.parametrize("branch", [
    {"backend": "hydra-core", "crashes": 0},
    {"backend": "hydra-core", "crashes": 0, "sample": {}},
    {"backend": "hydra-core", "crashes": 0, "sample": {"loo_accuracy": float("nan"),
                                                         "open_set_rejection": float("nan"), "false_merge_pairs": 0}},
    {**BASE_GATE, "backend": "stub+B3_rarity_bits"},
], ids=["missing-corpus", "missing-metrics", "nonfinite-metrics", "different-backend"])
def test_evaluation_gate_rejects_noncomparable_results(branch):
    assert compare(BASE_GATE, branch), "incomplete/noncomparable evaluation must not count as gate PASS"


def test_evaluation_gate_detects_reported_real_regression():
    assert compare(BASE_GATE, {**BASE_GATE, "sample": {"loo_accuracy": 0.5, "open_set_rejection": 1.0,
                                                    "false_merge_pairs": 0}})


def common_template_population():
    # Real public MinHash implementation; repeated text is shared boilerplate, independent
    # operator identities remain different. This isolates rarity handling, not language-model skill.
    from hydra.genome.txt import minhash, shingles
    from hydra.genome.txt_norm import fold

    text = " ".join(f"Syntetyczny katalog zawiera pozycję numer {i} i ogólny opis formularza." for i in range(80))
    features = shingles(fold(text).split(), 5)
    signature = "page:" + np.asarray(minhash(features).hashvalues, dtype=">u4").tobytes().hex()
    rows = {}
    for i in range(10):
        iid = f"template-{i}"
        common = gene(iid, "minhash_sig", signature, "TXT:phrase:minhash_page", "TXT")
        common.evidence = EvidencePointer(note=f"channel=page; shingles={len(features)}; synthetic common body")
        rows[iid] = [common, gene(iid, "tracker_id", f"independent-operator-{i}", "INF:operator:tracker")]
    return genomes(rows)


def test_non_discriminating_page_body_does_not_create_operator_campaign():
    population = common_template_population()
    rarity = rarity_for(population)
    pair = score_pair(population["template-0"], population["template-1"], rarity)
    assert pair.score.hypothesis != "same_campaign"
    assert pair.operator_bits == 0


def bridge_population():
    return genomes({"a": [gene("a", "tracker_id", "left-id", "INF:operator:tracker")],
                    "b": [gene("b", "tracker_id", "left-id", "INF:operator:tracker"),
                          gene("b", "landing_host", "right.example.test", "INF:operator:host")],
                    "c": [gene("c", "landing_host", "right.example.test", "INF:operator:host")]})


def test_fixed_membership_self_check_differs_from_rebuilt_holdout():
    population = bridge_population()
    rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    assignments, _conf, _summary = self_assign(population, clustering, rarity, TH)
    diagnostic = next(a for a in assignments if a.incident_id == "b")
    rest = {iid: g for iid, g in population.items() if iid != "b"}
    _rarity, _pairs, rebuilt, _info = score_and_cluster(rest, TH)
    held_out = assign(population["b"], rest, rebuilt.campaigns, rebuilt.groups).assignment
    assert diagnostic.decision == "assign"
    assert held_out.decision == "abstain"
    assert len(clustering.campaigns) == 1 and len(rebuilt.campaigns) == 2


def test_explicit_cannot_link_resolves_bridge():
    population = bridge_population()
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH, cannot_link=[("a", "c")])
    assert clustering.membership["a"] != clustering.membership["c"]


def population_fingerprint(population):
    payload = repr([(iid, [g.model_dump(mode="json") for g in genome.genes])
                    for iid, genome in sorted(population.items())])
    return hashlib.sha256(payload.encode()).hexdigest()


def test_small_calibration_reports_ungated_set_distinct_from_actual_decision():
    rows = {f"c{c}-{m}": identity(f"c{c}-{m}", c) for c in range(4) for m in range(2)}
    population = genomes(rows)
    _rarity, _pairs, clustering, _info = score_and_cluster(population, TH)
    result = calibrate(population, clustering.campaigns)
    assert result["n"] == 8 and not result["usable"]
    assert all(row["decision"] == "new_campaign" for row in result["held_out"])
    assert all(row["set_size"] > 0 for row in result["held_out"])
    assert result["held_out_rejection_rate"] == 0.0
