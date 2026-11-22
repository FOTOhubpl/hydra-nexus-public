"""SCORING-HARDEN controls (2026-11-21): the positive cases next to each CR-ASTRA fix, so a fix cannot pass by
switching evidence off. Synthetic genes only; the CR-ASTRA regressions themselves live in tests/adversarial."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from hydra.conformal import validate_calibration
from hydra.entropy import is_generic_form, is_placeholder, is_stop
from hydra.synapse import score_pair
from tests.adversarial.helpers import corpus, gene, image_unit


def test_placeholders_are_stop_genes_but_real_ids_are_not():
    for v in ("G-XXXXXXXXXX", "UA-XXXXX-Y", "UA-XXXXXXXX-1", "YOUR_PIXEL_ID", "{{PIXEL_ID}}", "GTM-XXXXXX",
              "ca-pub-0000000000000000", "123456789", "INSERT_TRACKING_ID"):
        assert is_placeholder("tracker_id", v), v
    for v in ("G-4K2J9QX7AB", "UA-12345678-1", "GTM-5ZR7XQ", "1234567890123456", "3456789", "pixel-42"):
        assert not is_placeholder("tracker_id", v), v
    assert not is_placeholder("phrase", "XXXXXXXX")  # identifier-like kinds only


def test_generic_form_fields_are_stop_genes():
    assert is_generic_form("form_fields", "name,phone") and is_stop("form_fields", "name,phone")
    assert is_generic_form("form_fields", "first_name,last_name,email,phone,country")
    assert is_generic_form("lead_form_kit", "hidden:_token")
    assert not is_generic_form("form_fields", "name,phone,sub1,offer_hash")
    assert not is_generic_form("post_fields", "name,phone")


def test_unfilled_snippet_plus_real_operator_still_links():
    rows = {iid: [gene(iid, "tracker_id", "G-XXXXXXXXXX"), gene(iid, "tracker_id", "synthetic-op-91", "INF:operator:t")]
            for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    pe = score_pair(genomes["a"], genomes["b"], rarity)
    assert pe.score.hypothesis == "same_campaign"
    assert all(r.value_a != "G-XXXXXXXXXX" for r in pe.score.reasons)


def test_quality_weight_scales_low_quality_and_keeps_good_observations():
    def pe_for(q):
        rows = {iid: [gene(iid, "tracker_id", "synthetic-rare-pixel", "INF:operator:tracker", quality=q)]
                for iid in ("a", "b")}
        genomes, rarity = corpus(rows)
        return score_pair(genomes["a"], genomes["b"], rarity)

    full, good, low, junk = pe_for(1.0), pe_for(0.6), pe_for(0.3), pe_for(0.2)
    assert full.score.total_bits == good.score.total_bits  # q >= 0.5 counts fully
    assert 0 < low.score.total_bits < full.score.total_bits
    assert low.score.total_bits == round(full.score.total_bits * 0.6, 3)
    assert low.score.hypothesis != "same_campaign"
    assert "niska jakość obserwacji q=0,30" in low.score.reasons[0].explanation_pl
    assert junk.score.total_bits == 0  # below QUALITY_MIN = 0.25: not evidence


def test_tier_conflict_is_explained():
    rows = {iid: [gene(iid, "tracker_id", "shared-default", "INF:operator:tracker"),
                  gene(iid, "tracker_id", "shared-default", "INF:boilerplate:default")] for iid in ("a", "b")}
    genomes, rarity = corpus(rows)
    pe = score_pair(genomes["a"], genomes["b"], rarity)
    notes = [r for r in pe.score.reasons if r.kind == "tier_conflict"]
    assert len(notes) == 1 and notes[0].bits == 0.0
    assert "sprzeczne oznaczenia" in notes[0].explanation_pl and "bit" in notes[0].explanation_pl


def test_calibration_problems_are_reported():
    alphas, eps, usable, problems = validate_calibration({"alphas": [-1.0] * 25, "eps": 0.1, "usable": False})
    assert usable and not problems and len(alphas) == 25 and eps == 0.1
    for bad in ({"alphas": [-1.0] * 24 + [float("nan")], "eps": 0.1}, {"alphas": [-1.0] * 25, "eps": 2},
                {"alphas": [-1.0] * 25, "eps": 0.1, "n": 30}, {"alphas": ["x"] * 25}, {"alphas": "[]"}):
        assert not validate_calibration(bad)[2], bad


def _page_image_rows(extra=None, scheme=None, holders=("a", "b")):
    rows, vectors = {}, {}
    for k, iid in enumerate(holders):
        # the same creative edited (crop/overlay): identical hash, embedding cos 0.92 < 0.95, so the standalone
        # near-duplicate rule does not fire and only the image-hash evidence remains
        vec = np.array([0.0, 1.0, 0.0, 0.0] if k == 0 else [0.4, 0.9165, 0.0, 0.0], dtype=np.float32)
        rows[iid], refs = image_unit(iid, f"{iid}-img", "c3d2a1b0e5f69788", "creative", vec / np.linalg.norm(vec))
        vectors.update(refs)
        if extra:
            rows[iid] += extra(iid)
        if scheme:
            rows[iid].append(gene(iid, "scheme", scheme[iid], "NAR:kit:scheme", "NAR"))
    return rows, vectors


def test_rare_shared_image_is_campaign_content_but_needs_a_second_family():
    """CR-AQ-1 (C): a rare near-identical image counts in the VIS campaign family (cap 8), never alone."""
    rows, vectors = _page_image_rows()
    genomes, rarity = corpus(rows, vectors)
    alone = score_pair(genomes["a"], genomes["b"], rarity)
    assert alone.score.hypothesis == "shared_asset" and alone.campaign_by_family.get("VIS", 0) > 0

    def text(iid):
        return [gene(iid, "phrase", "Syntetyczny bursztynowy kompas orbitalny", "TXT:phrase:rare", "TXT"),
                gene(iid, "phrase", "Lazurowa mapa hipotetycznej pracowni", "TXT:phrase:rare", "TXT")]

    rows, vectors = _page_image_rows(text)
    genomes, rarity = corpus(rows, vectors)
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis == "same_campaign"
    rows, vectors = _page_image_rows(text, holders=("a", "b", "c", "d"))  # stock: held by 4 incidents
    genomes, rarity = corpus(rows, vectors)
    stock = score_pair(genomes["a"], genomes["b"], rarity)
    assert stock.campaign_by_family.get("VIS", 0) == 0


def test_same_rare_image_suppresses_a_guessed_scheme_contradiction():
    """CR-AQ-1 (B): image-led pairs are not contradicted by a scheme guessed from little text."""
    rows, vectors = _page_image_rows(scheme={"a": "investment", "b": "fake_news_bait"})
    genomes, rarity = corpus(rows, vectors)
    assert score_pair(genomes["a"], genomes["b"], rarity).score.contradictions == []
    rows = {iid: [gene(iid, "scheme", s, "NAR:kit:scheme", "NAR"), gene(iid, "css_build_hash", "k", "INF:kit:css")]
            for iid, s in (("a", "investment"), ("b", "gambling"))}
    genomes, rarity = corpus(rows)
    pe = score_pair(genomes["a"], genomes["b"], rarity)
    assert pe.score.contradictions and "inwestycje" in pe.score.contradictions[0].explanation_pl


def test_new_pattern_reason_explains_the_family_rule_when_bits_pass_the_threshold():
    """CR-AQ-1 (iii): 12 TXT bits with no operator evidence is a new pattern, and the reason says why."""
    from hydra.assign import assign
    from hydra.contracts import Campaign

    phrases = ["Syntetyczny bursztynowy kompas orbitalny", "Lazurowa mapa hipotetycznej pracowni",
               "Fioletowy zegar nieistniejącej gwiazdy"]
    rows = {iid: [gene(iid, "phrase", p, "TXT:phrase:rare", "TXT").model_copy(
        update={"evidence": gene(iid, "phrase", p).evidence.model_copy(update={"artifact_id": f"{iid}-{k}"})})
        for k, p in enumerate(phrases)] for iid in ("a", "new")}
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    a = assign(new, genomes, [Campaign(id="C", label="Synthetic", members=["a"])], []).assignment
    assert a.decision == "new_campaign"
    assert "tylko z jednej rodziny cech (tekst)" in a.reason_pl and "ma tylko" not in a.reason_pl


def test_hub_value_scores_zero_bits():
    from hydra.entropy import build_rarity

    rows = {f"i{k}": [gene(f"i{k}", "tracker_id", "hub-pixel", "INF:operator:tracker")] for k in range(7)}
    rows.update({f"j{k}": [gene(f"j{k}", "host", f"h{k}.invalid", "INF:operator:host")] for k in range(3)})
    r = build_rarity(rows, {})
    assert r.hub_limit() == 5 and r.is_hub("tracker_id", "hub-pixel")
    assert r.discrete_bits("tracker_id", "hub-pixel", ("i0", "i1"))[0] == 0.0
    r.ignored = {("host", "h1.invalid")}
    assert r.discrete_bits("host", "h1.invalid", ())[0] == 0.0


def test_conformal_singleton_needs_the_link_rule():
    """GAP-AUDIT B4: with a usable calibration a one-campaign prediction set must not assign on weak evidence."""
    from hydra.assign import assign
    from hydra.contracts import Campaign

    rows = {iid: [gene(iid, "tracker_id", "synthetic-rare-pixel", "INF:operator:tracker", quality=0.3)]
            for iid in ("a", "new")}
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    campaigns = [Campaign(id="C", label="Synthetic", members=["a"])]
    plain = assign(new, genomes, campaigns, []).assignment
    assert plain.decision != "assign"
    res = assign(new, genomes, campaigns, [], conformal={"alphas": [-1.0] * 30, "eps": 0.1})
    assert res.conformal["usable"] and res.conformal["prediction_set"] == []  # gate: support < link (review M2)
    assert res.conformal["excluded_by_evidence_gate"] == ["C"]
    assert res.assignment.decision != "assign"
    assert res.conformal["coverage_band_5_95"] is not None and "wymienialności" in res.conformal["guarantee_pl"]


def test_conformal_never_promotes_a_threshold_new_pattern():
    """Review M2: an item the threshold rule calls a new pattern stays one whatever the calibration says."""
    from hydra.assign import assign
    from hydra.contracts import Campaign

    rows = {iid: [gene(iid, "ns_set", "ns1.synthetic-dns.invalid,ns2.synthetic-dns.invalid", "INF:operator:ns")]
            for iid in ("a", "new")}
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    campaigns = [Campaign(id="C", label="Synthetic", members=["a"])]
    plain = assign(new, genomes, campaigns, []).assignment
    res = assign(new, genomes, campaigns, [], conformal={"alphas": [-50.0] * 30, "eps": 0.1})
    assert plain.decision != "assign" and res.assignment.decision != "assign"


def _contact_rows(phrases, holders=("a", "b"), extra_phone_holders=()):
    rows = {}
    for iid in (*holders, *extra_phone_holders):
        rows[iid] = [gene(iid, "entity_phone", "+48700123456", "TXT:operator:phone", "TXT")]
        if iid in holders:
            rows[iid] += [gene(iid, "phrase", p, "TXT:phrase:rare", "TXT") for p in phrases]
    return rows


def test_contact_needs_two_rare_phrases_and_a_rare_contact():
    two = ["Syntetyczny bursztynowy kompas orbitalny", "Lazurowa mapa hipotetycznej pracowni"]
    genomes, rarity = corpus(_contact_rows(two))
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis == "same_campaign"
    genomes, rarity = corpus(_contact_rows(two[:1]))  # one phrase + phone: not enough
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis != "same_campaign"
    genomes, rarity = corpus(_contact_rows(two, extra_phone_holders=("c",)))  # phone held by 3 incidents
    assert score_pair(genomes["a"], genomes["b"], rarity).score.hypothesis != "same_campaign"


def _layout(seed):
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", (480, 320), (225, 215, 205))
    d = ImageDraw.Draw(im)
    for _ in range(80):
        x, y = rng.integers(0, 400), rng.integers(0, 260)
        d.rectangle((int(x), int(y), int(x + 50), int(y + 40)), fill=tuple(int(c) for c in rng.integers(20, 200, 3)))
    d.rectangle((15, 20, 310, 75), fill=(15, 45, 130))
    d.ellipse((320, 90, 460, 250), fill=(180, 50, 80))
    return im


def _image_pair(img_a, img_b):
    from hydra.genome.vis import phash_hex, unit_hash_values

    rows, vectors = {}, {}
    for iid, image in (("a", img_a), ("b", img_b)):
        rows[iid], refs = image_unit(iid, iid, phash_hex(image))
        for kind, (value, quality, _note) in unit_hash_values(image).items():
            if kind != "phash":
                rows[iid].append(rows[iid][0].model_copy(update={"kind": kind, "value": value, "quality": quality}))
        vectors.update(refs)
    genomes, rarity = corpus(rows, vectors)
    return score_pair(genomes["a"], genomes["b"], rarity)


def test_crop_evidence_does_not_link_different_images_of_one_template():
    # Same background layout, banner and ellipse, different rectangles; controlled cosine of one: the sub-window
    # hashes must still tell them apart (measured minimum distance 8 > 6 over 780 such pairs).
    for k in range(5):
        pe = _image_pair(_layout(200 + k), _layout(300 + k))
        assert pe.score.hypothesis != "same_campaign", k


def test_flipped_creative_matches_through_dihedral_pdq():
    from hydra.genome.vis import pdq_dihedral

    original = _layout(7)
    mirrored = ImageOps.mirror(original)
    pe = _image_pair(original, mirrored)
    assert pe.score.hypothesis == "same_campaign"
    ha, _ = pdq_dihedral(original)
    hb, _ = pdq_dihedral(mirrored)
    assert min(bin(int(x, 16) ^ int(hb[0], 16)).count("1") for x in ha) <= 31


def test_operator_bits_do_not_fall_with_campaign_size():
    """Review M1: a new item sharing one operator tracker with a known campaign of k members keeps >= 10 bits."""
    from hydra.assign import assign
    from hydra.contracts import Campaign

    for k in (1, 2, 5, 10):
        rows = {f"m{j}": [gene(f"m{j}", "tracker_id", "synthetic-op-31337", "INF:operator:tracker")] for j in range(k)}
        rows["new"] = [gene("new", "tracker_id", "synthetic-op-31337", "INF:operator:tracker")]
        genomes, _ = corpus(rows)
        new = genomes.pop("new")
        res = assign(new, genomes, [Campaign(id="C", label="Synthetic", members=[f"m{j}" for j in range(k)])], [])
        best = max(res.pairs.values(), key=lambda p: p.support)
        assert best.support >= 10, (k, best.support)
        assert res.assignment.decision == "assign" and res.assignment.campaign_id == "C", (k, res.assignment.reason_pl)


def test_operator_value_in_three_known_campaigns_is_platform():
    """Review M1 spread rule: one operator value in members of >= 3 known campaigns identifies none of them."""
    from hydra.assign import assign
    from hydra.contracts import Campaign

    rows = {f"c{j}": [gene(f"c{j}", "tracker_id", "shared-agency-pixel", "INF:operator:tracker")] for j in range(3)}
    rows["new"] = [gene("new", "tracker_id", "shared-agency-pixel", "INF:operator:tracker")]
    genomes, _ = corpus(rows)
    new = genomes.pop("new")
    camps = [Campaign(id=f"C{j}", label="Synthetic", members=[f"c{j}"]) for j in range(3)]
    res = assign(new, genomes, camps, [])
    assert res.assignment.decision == "new_campaign"
    assert all(p.support == 0 for p in res.pairs.values())


def test_one_domain_in_three_host_genes_is_one_group():
    """Review M7: the landing domain seen as landing host, contacted host and linked host counts once."""
    one = {iid: [gene(iid, "landing_host", "promo.synthetic-lure.invalid", "INF:operator:landing")]
           for iid in ("a", "b")}
    three = {iid: [gene(iid, "landing_host", "promo.synthetic-lure.invalid", "INF:operator:landing"),
                   gene(iid, "host_contacted", "promo.synthetic-lure.invalid", "INF:operator:hosts"),
                   gene(iid, "linked_host", "cdn.synthetic-lure.invalid", "INF:operator:linked_hosts")]
             for iid in ("a", "b")}
    g1, r1 = corpus(one)
    g3, r3 = corpus(three)
    p1, p3 = score_pair(g1["a"], g1["b"], r1), score_pair(g3["a"], g3["b"], r3)
    assert p3.operator_bits <= 14.0 + 1e-9
    assert p3.operator_bits < 2 * p1.operator_bits


def test_dense_large_component_is_not_split():
    """Review M8: a dense component of 70 same-campaign incidents (n = 100) stays one campaign; the old guard
    max(25, n // 4) = 25 split it."""
    import copy

    from hydra.nexus import cluster

    rows = {f"i{k:02d}": [gene(f"i{k:02d}", "tracker_id", "synthetic-op-777", "INF:operator:tracker")]
            for k in range(70)}
    genomes, rarity = corpus(rows, background=30)
    link = score_pair(genomes["i00"], genomes["i01"], *corpus({x: rows[x] for x in ("i00", "i01")})[1:])
    assert link.score.hypothesis == "same_campaign"
    ids = sorted(rows)
    pairs = {}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            pe = copy.deepcopy(link)
            pe.score.a, pe.score.b = a, b
            pairs[(a, b)] = pe
    res = cluster(genomes, pairs, rarity)
    assert len({res.membership[i] for i in ids}) == 1


def test_byte_identical_incidents_are_one_rarity_unit():
    """Review M9 / CR-VP-1: three byte-identical incidents plus background form one campaign."""
    from hydra.pipeline import score_and_cluster
    from hydra.synapse import Thresholds, build_genome

    rows = {}
    for iid in ("d1", "d2", "d3"):
        rows[iid] = [gene(iid, "artifact_sha256", "ab" * 32, "core:vis_files", "INF"),
                     gene(iid, "tracker_id", "synthetic-dup-pixel", "INF:operator:tracker")]
    for k in range(8):
        rows[f"bg{k}"] = [gene(f"bg{k}", "host", f"bg{k}.invalid", "INF:operator:host")]
    genomes = {i: build_genome(i, gs, {}) for i, gs in rows.items()}
    _r, _p, cl, _ = score_and_cluster(genomes, Thresholds())
    assert len({cl.membership[i] for i in ("d1", "d2", "d3")}) == 1


def test_campaign_creative_is_not_stock_during_assignment():
    """M1 applied to images: a creative held by the three members of one known campaign is that campaign's own
    image, not stock, when a new copy is assigned (near-hash frequency counts known campaigns)."""
    from hydra.assign import assign
    from hydra.contracts import Campaign

    rows, vectors = {}, {}
    for iid in ("m0", "m1", "m2", "new"):
        rows[iid], refs = image_unit(iid, f"{iid}-ad", "95aabbccddeeff17", "creative")
        vectors.update(refs)
    genomes, _ = corpus(rows, vectors)
    new = genomes.pop("new")
    res = assign(new, genomes, [Campaign(id="C", label="Synthetic", members=["m0", "m1", "m2"])], [])
    assert res.assignment.decision == "assign" and res.assignment.campaign_id == "C", res.assignment.reason_pl


def _page_body_rows(n):
    from hydra.contracts import EvidencePointer
    from hydra.genome.txt import minhash, shingles
    from hydra.genome.txt_norm import fold

    text = " ".join(f"Syntetyczny katalog zawiera pozycję numer {i} i ogólny opis formularza." for i in range(80))
    feats = shingles(fold(text).split(), 5)
    sig = "page:" + np.asarray(minhash(feats).hashvalues, dtype=">u4").tobytes().hex()
    rows = {}
    for i in range(n):
        iid = f"tpl-{i}"
        g = gene(iid, "minhash_sig", sig, "TXT:phrase:minhash_page", "TXT")
        g.evidence = EvidencePointer(note=f"channel=page; shingles={len(feats)}; synthetic body")
        rows[iid] = [g, gene(iid, "tracker_id", f"independent-operator-{i}", "INF:operator:tracker")]
    return rows


def test_template_page_body_does_not_link_independent_operators():
    """CR-ASTRA-PIPE-203: a near-identical body held by ten incidents is a template (0 bits); two copies still link."""
    genomes, rarity = corpus(_page_body_rows(10), background=0)
    pe = score_pair(genomes["tpl-0"], genomes["tpl-1"], rarity)
    assert pe.score.hypothesis != "same_campaign" and pe.operator_bits == 0
    assert any(r.kind == "text_template" for r in pe.score.reasons)
    genomes, rarity = corpus(_page_body_rows(2))
    assert score_pair(genomes["tpl-0"], genomes["tpl-1"], rarity).score.hypothesis == "same_campaign"


def test_hub_cap_keeps_a_large_campaign_in_batch_scoring():
    """Incident units: 13 holders of one value in a 60-incident corpus are not a hub (cap = half the corpus)."""
    from hydra.entropy import build_rarity

    rows = {f"i{k:02d}": [gene(f"i{k:02d}", "host", f"own-{k}.invalid", "INF:operator:own")] for k in range(60)}
    for k in range(13):
        rows[f"i{k:02d}"].append(gene(f"i{k:02d}", "tracker_id", "big-campaign-pixel", "INF:operator:tracker"))
    r = build_rarity(rows, {})
    assert r.discrete_bits("tracker_id", "big-campaign-pixel", ("i00", "i01"), operator=True)[0] > 0


REPRESENTATIONS = [
    ("landing_host", "promo.synthetic-lure.invalid", "INF:operator:landing", "INF"),
    ("host_contacted", "promo.synthetic-lure.invalid", "INF:operator:hosts", "INF"),
    ("linked_host", "cdn.synthetic-lure.invalid", "INF:operator:linked_hosts", "INF"),
    ("domain_in_text", "synthetic-lure.invalid", "TXT:operator:domain", "TXT"),
    ("url_in_text", "https://promo.synthetic-lure.invalid/oferta", "TXT:operator:url", "TXT"),
]


def test_more_representations_of_one_fact_never_promote():
    """Operator verification (a): one domain read from HTML, HAR and OCR is one fact. For every subset of its
    representations the pair is same_campaign only if one representation alone already is, and the support never
    exceeds the strongest single representation by more than the dependency-group rest weight."""
    from itertools import combinations as comb

    from hydra.synapse import GROUP_CAP

    def pe_for(reps):
        rows = {iid: [gene(iid, k, v, grp, fam) for k, v, grp, fam in reps] for iid in ("a", "b")}
        genomes, rarity = corpus(rows)
        return score_pair(genomes["a"], genomes["b"], rarity)

    single = {r: pe_for([r]) for r in REPRESENTATIONS}
    for n in range(2, len(REPRESENTATIONS) + 1):
        for subset in comb(REPRESENTATIONS, n):
            pe = pe_for(list(subset))
            best = max(single[r].support for r in subset)
            if pe.score.hypothesis == "same_campaign":
                assert any(single[r].score.hypothesis == "same_campaign" for r in subset), subset
            assert pe.support <= min(GROUP_CAP, best * (1 + 0.25 * (n - 1))) + 1e-6, (subset, pe.support, best)
            assert sum(1 for v in pe.campaign_by_family.values() if v > 0) == 1, subset


def test_stock_bridge_does_not_chain_two_operators_but_rotation_stays_one_campaign():
    """Operator verification (c): similar graph topology, opposite truth. A stock creative shared by members of two
    operators (held by four incidents) is no bridge; one operator rotating domains with an overlapping tracker chain
    stays one campaign. No blanket rule cuts every bridge."""
    from itertools import combinations as comb

    from hydra.nexus import cluster

    rows, vectors = {}, {}
    for iid, op in (("a1", "op-a"), ("a2", "op-a"), ("b1", "op-b"), ("b2", "op-b")):
        rows[iid] = [gene(iid, "tracker_id", f"synthetic-{op}", "INF:operator:tracker")]
        if iid in ("a2", "b1", "a1", "b2"):
            unit, refs = image_unit(iid, f"{iid}-stock", "c3d2a1b0e5f69788", "creative")
            rows[iid] += unit
            vectors.update(refs)
    genomes, rarity = corpus(rows, vectors)
    pairs = {(x, y): score_pair(genomes[x], genomes[y], rarity) for x, y in comb(sorted(genomes), 2)}
    res = cluster(genomes, pairs, rarity)
    assert res.membership["a1"] == res.membership["a2"] and res.membership["b1"] == res.membership["b2"]
    assert res.membership["a1"] != res.membership["b1"]

    chain = {"r1": [("tracker_id", "rot-t1"), ("landing_host", "d1.synthetic-rot.invalid")],
             "r2": [("tracker_id", "rot-t1"), ("tracker_id", "rot-t2"), ("landing_host", "d2.synthetic-rot2.invalid")],
             "r3": [("tracker_id", "rot-t2"), ("tracker_id", "rot-t3"), ("landing_host", "d3.synthetic-rot3.invalid")],
             "r4": [("tracker_id", "rot-t3"), ("landing_host", "d4.synthetic-rot4.invalid")]}
    rows = {iid: [gene(iid, k, v, "INF:operator:" + k) for k, v in gs] for iid, gs in chain.items()}
    genomes, rarity = corpus(rows)
    pairs = {(x, y): score_pair(genomes[x], genomes[y], rarity) for x, y in comb(sorted(genomes), 2)}
    res = cluster(genomes, pairs, rarity)
    assert len({res.membership[i] for i in chain}) == 1
