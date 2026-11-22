"""Metamorphic properties; deterministic Hypothesis generation, no example database."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from hydra.contracts import Family, GeneStatus
from hydra.synapse import FAMILY_CAMPAIGN_CAP, FAMILY_CAP, score_pair
from tests.adversarial.helpers import PROPERTY, corpus, gene, image_unit, scored_cluster


@PROPERTY
@given(st.lists(st.integers(2, 100_000), min_size=1, max_size=15, unique=True))
def test_score_is_symmetric_and_evidence_sides_stay_canonical(values):
    rows = {iid: [gene(iid, "tracker_id", f"pixel-{v}", f"INF:operator:g{k}")
                  for k, v in enumerate(values)] for iid in ("a", "z")}
    genomes, rarity = corpus(rows)
    ab = score_pair(genomes["a"], genomes["z"], rarity)
    ba = score_pair(genomes["z"], genomes["a"], rarity)
    assert ab.score.model_dump() == ba.score.model_dump()
    assert (ab.campaign_bits, ab.kit_bits, ab.asset_bits, ab.support) == (
        ba.campaign_bits, ba.kit_bits, ba.asset_bits, ba.support)
    for reason in ab.score.reasons:
        if reason.evidence_a is not None:
            assert reason.evidence_a in [g.evidence for g in rows["a"]]
        if reason.evidence_b is not None:
            assert reason.evidence_b in [g.evidence for g in rows["z"]]


@PROPERTY
@given(st.integers(0, 30), st.integers(2, 100_000))
def test_adding_a_rare_shared_operator_never_lowers_bits(n, value):
    rows = {iid: [gene(iid, "tracker_id", f"pixel-{k}", f"INF:operator:tracker-{k}") for k in range(n)]
            for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    before = score_pair(genomes["a"], genomes["b"], rarity)
    for iid in rows:
        rows[iid].append(gene(iid, "operator_id", f"rare-{value}", "INF:operator:extra"))
    genomes, rarity = corpus(rows)
    after = score_pair(genomes["a"], genomes["b"], rarity)
    assert after.score.total_bits >= before.score.total_bits
    assert after.campaign_bits >= before.campaign_bits
    assert after.support >= before.support


@pytest.mark.parametrize("family", list(Family))
@PROPERTY
@given(st.integers(1, 40))
def test_family_caps_hold_when_independent_groups_are_flooded(family, n):
    rows = {iid: [gene(iid, "tracker_id", f"pixel-{k}", f"{family}:operator:g{k}", family) for k in range(n)]
            for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    pair = score_pair(genomes["a"], genomes["b"], rarity)
    assert all(0 <= value <= FAMILY_CAP for value in pair.score.family_bits.values())
    assert pair.campaign_by_family.get(str(family), 0) <= FAMILY_CAMPAIGN_CAP[str(family)]


@pytest.mark.parametrize("status", [GeneStatus.UNAVAILABLE, GeneStatus.FAILED, GeneStatus.NOT_APPLICABLE])
def test_missing_observations_are_unknown_without_score_penalty(status):
    rows = {iid: [gene(iid, "tracker_id", "rare-pixel", "INF:operator:tracker")] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    before = score_pair(genomes["a"], genomes["b"], rarity)
    for iid in rows:
        rows[iid].extend([
            gene(iid, "phrase", "unobserved text", "TXT:phrase:body", "TXT", status=status),
            gene(iid, "scheme", "investment" if iid == "a" else "health", "NAR:kit:scheme", "NAR", status=status),
        ])
    genomes, rarity = corpus(rows)
    after = score_pair(genomes["a"], genomes["b"], rarity)
    assert before.score.model_dump() == after.score.model_dump()
    assert {"TXT", "NAR"} <= set(after.score.missing_families)
    assert after.score.contradictions == []


@PROPERTY
@given(st.permutations(["a", "b", "c", "d"]), st.booleans())
def test_independent_runs_and_input_permutations_are_deterministic(order, reverse):
    rows = {iid: [gene(iid, "tracker_id", f"pixel-{0 if iid in 'ab' else 1}", "INF:operator:tracker"),
                  gene(iid, "css_build_hash", "common-kit", "INF:kit:css")]
            for iid in "abcd"}

    def result(ids, flip=False):
        gs = {iid: list(reversed(rows[iid])) if flip else rows[iid] for iid in ids}
        genomes, rarity = corpus(gs)
        pairs, clustering = scored_cluster(genomes, rarity)
        return ({key: pair.score.model_dump() for key, pair in pairs.items()},
                [c.model_dump() for c in clustering.campaigns],
                [g.model_dump() for g in clustering.groups])

    assert result("abcd") == result(order, reverse) == result("abcd")


def test_duplicate_gene_rows_do_not_inflate_evidence():
    rows = {iid: [gene(iid, "tracker_id", "rare-pixel", "INF:operator:tracker")] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    expected = score_pair(genomes["a"], genomes["b"], rarity).score.model_dump()
    genomes, rarity = corpus({iid: gs * 50 for iid, gs in rows.items()})
    assert score_pair(genomes["a"], genomes["b"], rarity).score.model_dump() == expected


def test_conflicting_tier_annotations_do_not_depend_on_input_gene_order():
    rows = {iid: [
        gene(iid, "tracker_id", "shared-default", "INF:operator:tracker"),
        gene(iid, "tracker_id", "shared-default", "INF:boilerplate:default"),
    ] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    before = score_pair(genomes["a"], genomes["b"], rarity)
    genomes, rarity = corpus({iid: list(reversed(gs)) for iid, gs in rows.items()})
    after = score_pair(genomes["a"], genomes["b"], rarity)
    assert before.score.hypothesis == after.score.hypothesis
    assert before.score.total_bits == after.score.total_bits


def test_bits_and_uncalibrated_probability_are_distinct_in_public_results():
    rows = {iid: [gene(iid, "tracker_id", "rare-pixel", "INF:operator:tracker")] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    pair = score_pair(genomes["a"], genomes["b"], rarity).score
    assert pair.total_bits > 1.0
    assert pair.calibrated is False and 0 <= pair.probability <= 1
    assert pair.model == "rarity_bits@1"
    assert all("bit" in reason.explanation_pl for reason in pair.reasons)
    assert all("%" not in reason.explanation_pl for reason in pair.reasons)
    assert np.isfinite(pair.total_bits)


def test_incident_renaming_cannot_change_visual_attribution():
    def score(many, one):
        rows, vectors = {many: [], one: []}, {}
        for iid, count in ((many, 5), (one, 1)):
            for k in range(count):
                gs, refs = image_unit(iid, f"unit-{k}", "95aabbccddeeff17", "page_image")
                rows[iid].extend(gs)
                vectors.update(refs)
        genomes, rarity = corpus(rows, vectors)
        return score_pair(genomes[many], genomes[one], rarity)

    first, renamed = score("a", "b"), score("b", "a")
    assert first.score.hypothesis == renamed.score.hypothesis
    assert first.support == renamed.support


@pytest.mark.parametrize("value", ["not-hex", "f", "", "ff" * 200])
def test_malformed_perceptual_hash_is_unknown_not_a_crash(value):
    rows = {iid: [gene(iid, "phash", value, "vis_unit:image", "VIS")] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    pair = score_pair(genomes["a"], genomes["b"], rarity)
    assert pair.score.hypothesis != "same_campaign"
    assert pair.score.total_bits == 0
