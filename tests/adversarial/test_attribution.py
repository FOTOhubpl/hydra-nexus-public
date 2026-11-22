"""Negative attribution cases: common features must not identify an operator."""

from itertools import combinations

import pytest

from hydra.synapse import score_pair
from tests.adversarial.helpers import corpus, gene, image_unit, scored_cluster


@pytest.mark.parametrize(("kind", "value", "group", "family"), [
    ("host", "cdnjs.cloudflare.com", None, "INF"),
    ("asn", "13335", "INF:boilerplate:cdn_asn", "INF"),
    ("ip", "192.0.2.10", "INF:boilerplate:cdn_ip", "INF"),
    ("tracker_id", "G-XXXXXXXXXX", "INF:boilerplate:default_tracker", "INF"),
    ("dom_skeleton", "wordpress-generic-layout", "INF:kit:cms", "INF"),
    ("phrase", "Ta strona korzysta z plików cookies.", "TXT:boilerplate:cookie_banner", "TXT"),
    ("brand_mention", "Syntetyczny Bank", "TXT:persona:brand", "TXT"),
    ("logo", "synthetic-bank-logo", "ID:asset:logo", "ID"),
], ids=["cdn", "asn", "shared-ip", "tagged-default-tracker", "cms", "cookie-banner", "brand", "logo"])
def test_common_feature_does_not_create_campaign(kind, value, group, family):
    rows = {iid: [gene(iid, kind, value, group, family)] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    pair = score_pair(genomes["a"], genomes["b"], rarity)
    assert pair.score.hypothesis != "same_campaign", pair
    assert pair.operator_bits == 0, pair


@pytest.mark.parametrize("tracker", ["G-XXXXXXXXXX", "UA-XXXXX-Y", "YOUR_PIXEL_ID"])
def test_default_tracker_without_boilerplate_annotation_is_not_an_operator(tracker):
    # An absent tier must not turn an obvious SDK placeholder into a rare operator identity.
    rows = {iid: [gene(iid, "tracker_id", tracker)] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis != "same_campaign"


def test_many_common_features_cannot_accumulate_into_campaign():
    rows = {}
    for iid in ("a", "b"):
        rows[iid] = [
            gene(iid, "tracker_id", "G-XXXXXXXXXX", "INF:boilerplate:tracker"),
            gene(iid, "asn", "13335", "INF:boilerplate:cdn"),
            gene(iid, "dom_skeleton", "wordpress", "INF:kit:cms"),
            gene(iid, "phrase", "Akceptuję pliki cookies", "TXT:boilerplate:cookie", "TXT"),
            gene(iid, "brand_mention", "Syntetyczna Marka", "TXT:persona:brand", "TXT"),
        ]
    genomes, rarity = corpus(rows)
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis != "same_campaign"


def test_stock_creative_present_in_many_incidents_never_merges_them():
    rows, vectors = {}, {}
    for k in range(6):
        iid = f"stock-{k}"
        rows[iid], refs = image_unit(iid, f"photo-{k}", "95aabbccddeeff17")
        vectors.update(refs)
    genomes, rarity = corpus(rows, vectors)
    pairs, result = scored_cluster(genomes, rarity)
    assert all(p.score.hypothesis != "same_campaign" for p in pairs.values())
    assert all(len(c.members) == 1 for c in result.campaigns)


def test_distinct_weak_bridges_do_not_chain_campaigns():
    rows = {
        "a": [gene("a", "css_build_hash", "kit-ab", "INF:kit:css")],
        "b": [gene("b", "css_build_hash", "kit-ab", "INF:kit:css"),
              gene("b", "js_build_hash", "kit-bc", "INF:kit:js")],
        "c": [gene("c", "js_build_hash", "kit-bc", "INF:kit:js")],
    }
    genomes, rarity = corpus(rows)
    pairs, result = scored_cluster(genomes, rarity)
    assert pairs["a", "b"].kit_bits > 0 and pairs["b", "c"].kit_bits > 0
    assert pairs["a", "c"].score.hypothesis == "independent"
    assert len({result.membership[iid] for iid in rows}) == 3


def test_multiple_copies_of_one_page_image_are_not_five_independent_matches():
    rows, vectors = {"a": [], "b": []}, {}
    for iid, count in (("a", 5), ("b", 1)):
        for k in range(count):
            genes, refs = image_unit(iid, f"{iid}-unit-{k}", "95aabbccddeeff17", "page_image")
            rows[iid].extend(genes)
            vectors.update(refs)
    genomes, rarity = corpus(rows, vectors)
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis != "same_campaign"


def test_zero_quality_shared_operator_is_not_campaign_evidence():
    rows = {iid: [gene(iid, "tracker_id", "synthetic-rare-pixel", "INF:operator:tracker", quality=0.0)]
            for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis != "same_campaign"


def test_a_common_brand_does_not_merge_two_independently_supported_campaigns():
    rows = {}
    for iid, operator in (("a1", "pixel-a"), ("a2", "pixel-a"), ("b1", "pixel-b"), ("b2", "pixel-b")):
        rows[iid] = [gene(iid, "tracker_id", operator, "INF:operator:tracker"),
                     gene(iid, "brand_mention", "Syntetyczna Marka", "TXT:persona:brand", "TXT")]
    genomes, rarity = corpus(rows)
    pairs, result = scored_cluster(genomes, rarity)
    assert result.membership["a1"] == result.membership["a2"]
    assert result.membership["b1"] == result.membership["b2"]
    assert result.membership["a1"] != result.membership["b1"]
    for a, b in combinations(sorted(rows), 2):
        if a[0] != b[0]:
            assert pairs[a, b].score.hypothesis != "same_campaign"


def test_near_identical_stock_variants_are_common_even_without_exact_hash_collisions():
    rows, vectors = {}, {}
    for k in range(6):
        iid = f"stock-{k}"
        # Different encodings of the same stock content: pairwise Hamming distance two, cosine one.
        phash = f"{int('95aabbccddeeff17', 16) ^ (1 << k):016x}"
        rows[iid], refs = image_unit(iid, iid, phash)
        vectors.update(refs)
    genomes, rarity = corpus(rows, vectors)
    pairs, result = scored_cluster(genomes, rarity)
    assert all(p.score.hypothesis != "same_campaign" for p in pairs.values())
    assert all(len(c.members) == 1 for c in result.campaigns)


def test_different_default_trackers_do_not_chain_unrelated_incidents():
    rows = {
        "a": [gene("a", "tracker_id", "G-XXXXXXXXXX")],
        "b": [gene("b", "tracker_id", "G-XXXXXXXXXX"), gene("b", "tracker_id", "UA-XXXXX-Y")],
        "c": [gene("c", "tracker_id", "UA-XXXXX-Y")],
    }
    genomes, rarity = corpus(rows)
    pairs, result = scored_cluster(genomes, rarity)
    assert pairs["a", "c"].score.hypothesis == "independent"
    assert result.membership["a"] != result.membership["c"]
