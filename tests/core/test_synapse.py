"""Scorer unit tests on hand-built genomes."""

import numpy as np

from hydra.contracts import Family, Gene
from hydra.entropy import build_postings, build_rarity, seed_units
from hydra.synapse import PLATFORM_KIT_CAP, build_genome, level, score_pair


def g(iid, kind, value, group=None, fam=Family.INF):
    return Gene(incident_id=iid, family=fam, kind=kind, value=value, extractor="t@1", dependency_group=group)


def corpus(extra_a=(), extra_b=()):
    genes = {f"i{k}": [g(f"i{k}", "host", f"h{k}.example")] for k in range(8)}
    genes["i0"] += list(extra_a)
    genes["i1"] += list(extra_b)
    rarity = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    return gen, rarity


def test_tiers_from_dependency_group():
    assert level(g("x", "landing_cdn_account", "a", "INF:operator:aio_acct")) == "campaign"
    assert level(g("x", "param_shape", "a", "INF:platform:utm")) == "kit"
    assert level(g("x", "ip", "1.1.1.1", "INF:boilerplate:cdn_ip")) == "none"
    assert level(g("x", "cert_pl_listed", "civic.example")) == "none"
    assert level(g("x", "face", "f1", fam=Family.ID)) == "asset"


def test_platform_genes_are_capped_and_cannot_link_alone():
    plat = [g("i0", f"p{k}", "v", f"INF:platform:p{k}") for k in range(5)]
    plat_b = [g("i1", f"p{k}", "v", f"INF:platform:p{k}") for k in range(5)]
    gen, rarity = corpus(plat, plat_b)
    pe = score_pair(gen["i0"], gen["i1"], rarity)
    assert pe.kit_bits > 10 and pe.score.total_bits <= PLATFORM_KIT_CAP + 1e-6 and pe.support == 0
    cap = [r for r in pe.score.reasons if r.kind == "cap_correction"]
    assert cap and cap[0].bits < 0 and cap[0].explanation_pl.startswith("Limit dla dowodów platformy")
    assert abs(sum(pe.score.family_bits.values()) + cap[0].bits - pe.score.total_bits) < 1e-2
    assert pe.score.hypothesis == "shared_template"


def test_operator_gene_links_and_boilerplate_is_zero():
    op = g("i0", "landing_cdn_account", "a1b2c3d4", "INF:operator:aio_acct")
    gen, rarity = corpus([op, g("i0", "ip", "192.0.2.12", "INF:boilerplate:cdn")],
                         [g("i1", "landing_cdn_account", "a1b2c3d4", "INF:operator:aio_acct"),
                          g("i1", "ip", "192.0.2.12", "INF:boilerplate:cdn")])
    pe = score_pair(gen["i0"], gen["i1"], rarity)
    assert pe.score.hypothesis == "same_campaign"
    assert all(r.kind != "ip" for r in pe.score.reasons)
    assert pe.score.reasons[0].explanation_pl.startswith("Wspólna cecha")


def test_seed_units_need_two_groups():
    genes = {"a": [g("a", "host", "x.example", "INF:operator:host"), g("a", "entity_phone", "+48", fam=Family.TXT)],
             "b": [g("b", "host", "x.example", "INF:operator:host"), g("b", "entity_phone", "+48", fam=Family.TXT)],
             "c": [g("c", "host", "x.example", "INF:operator:host")]}
    u = seed_units(genes, build_postings(genes))
    assert u["a"] == u["b"] != u["c"]


def test_knn_masks_own_vectors_before_ranking():
    import numpy as np

    from hydra.retrieval import knn_incident_pairs

    rng = np.random.default_rng(0)
    base = rng.normal(size=16)
    frames = np.stack([base + 0.01 * rng.normal(size=16) for _ in range(50)])  # one video: 50 near-identical frames
    other = base + 0.05 * rng.normal(size=16)
    x = np.vstack([frames, other[None], rng.normal(size=(5, 16))]).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    own = np.array(["vid"] * 50 + ["twin"] + [f"r{i}" for i in range(5)])
    pairs = knn_incident_pairs(x, own, k=1, cut=0.9)
    assert ("vid", "twin") in pairs  # plain top-1 per row would only find the video's own frames
    q = knn_incident_pairs(x, own, k=1, cut=0.9, queries={"twin"})
    assert q == [("twin", "vid")]


def _mh(iid, sig, shingles=400):
    from hydra.contracts import EvidencePointer

    return Gene(incident_id=iid, family=Family.TXT, kind="minhash_sig",
                value="page:" + np.asarray(sig, dtype=">u4").tobytes().hex(), extractor="t@1",
                dependency_group="TXT:phrase:minhash_page",
                evidence=EvidencePointer(note=f"channel=page; shingles={shingles}; perm=128"))


def test_text_near_duplicate_links_alone_but_not_short_or_dissimilar_text():
    rng = np.random.default_rng(1)
    base = rng.integers(0, 2**32, 128, dtype=np.uint64).astype(np.uint32)
    near = base.copy()
    near[:5] += 1  # Jaccard ~ 0.96
    far = base.copy()
    far[:40] += 1  # ~ 0.69
    for other, shingles, want in ((near, 400, "same_campaign"), (far, 400, None), (near, 20, None)):
        gen, r = corpus([_mh("i0", base, shingles)], [_mh("i1", other, shingles)])
        pe = score_pair(gen["i0"], gen["i1"], r)
        nd = [m for m in pe.score.reasons if m.kind == "text_near_duplicate"]
        assert (pe.score.hypothesis == "same_campaign") == (want is not None) and bool(nd) == (want is not None)


def _unit(iid, uid, cls, phash, vec):
    from hydra.contracts import EvidencePointer

    ev = EvidencePointer(note=f"unit={uid}; class={cls}; kind=image")
    return [Gene(incident_id=iid, family=Family.VIS, kind="phash", value=phash, extractor="t@1",
                 dependency_group=f"vis_unit:{uid}", evidence=ev),
            Gene(incident_id=iid, family=Family.VIS, kind="dinov2", vector_ref=f"{iid}|{uid}|dinov2|0", extractor="t@1",
                 dependency_group=f"vis_unit:{uid}", evidence=ev)], {f"{iid}|{uid}|dinov2|0": ("dino@1", vec)}


def test_visual_near_duplicate_creative_links_alone_avatar_and_single_page_image_do_not():
    from hydra.entropy import build_rarity

    # Five DIFFERENT page images (distinct hashes and vectors), each present in both incidents. Near-identical
    # copies inside one incident count once (CR-ASTRA-2), so the units must differ from each other.
    rng = np.random.default_rng(5)
    hashes = [f"{int(x):016x}" for x in rng.integers(0, 2**63, 5)]
    vectors = [np.eye(8, dtype=np.float32)[u] for u in range(5)]
    for cls, n_units, want in (("creative", 1, True), ("avatar", 1, False), ("page_image", 1, False),
                               ("page_image", 5, True)):
        genes, vecs = {}, {}
        for iid in ("i0", "i1"):
            genes[iid] = []
            for u in range(n_units):
                gs, vs = _unit(iid, f"u{u}{iid}", cls, hashes[u], vectors[u])
                genes[iid] += gs
                vecs.update(vs)
        for k in range(2, 8):
            genes[f"i{k}"] = [g(f"i{k}", "host", f"h{k}")]
        gen = {i: build_genome(i, gs, vecs) for i, gs in genes.items()}
        pe = score_pair(gen["i0"], gen["i1"], build_rarity(genes, {}))
        assert (pe.score.hypothesis == "same_campaign") == want, (cls, n_units, pe.score.hypothesis)


def _kit_corpus(n, chain=True, common=True):
    genes = {f"i{k:02d}": [g(f"i{k:02d}", "host", f"h{k}.example", "INF:operator:host")] for k in range(n)}
    for k in range(n):
        if common:  # one platform value shared by everyone (df = n)
            genes[f"i{k:02d}"].append(g(f"i{k:02d}", "api_path", "/api/v1/trigger", "INF:platform:endpoints"))
        if chain:  # two independent rare kit genes shared with the next incident only
            for j in (k, k - 1):
                if 0 <= j < n - 1:
                    genes[f"i{k:02d}"].append(g(f"i{k:02d}", "css_build_hash", f"css{j}", "INF:kit:css_build"))
                    genes[f"i{k:02d}"].append(g(f"i{k:02d}", "js_build_hash", f"js{j}", "INF:kit:js_build"))
    return genes


def _cluster(genes, **kw):
    from itertools import combinations

    from hydra.nexus import cluster

    r = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    pairs = {(a, b): score_pair(gen[a], gen[b], r) for a, b in combinations(sorted(gen), 2)}
    return cluster(gen, pairs, r, **kw)


def test_one_common_kit_value_never_chains_a_group():
    from types import SimpleNamespace

    from hydra.nexus import group_link_supported

    assert _cluster(_kit_corpus(20, chain=False, common=True)).groups == []
    pe = lambda n, df: SimpleNamespace(kit_groups=n, kit_min_df=df)  # noqa: E731
    assert not group_link_supported(pe(1, 1431)) and not group_link_supported(pe(0, None))
    assert group_link_supported(pe(1, 2)) and group_link_supported(pe(2, 900))


def test_two_independent_kit_genes_link_and_oversized_groups_are_split():
    cl = _cluster(_kit_corpus(6, chain=True, common=False))
    assert len(cl.groups) == 1 and len(cl.groups[0].campaign_ids) == 6
    assert cl.group_meta[cl.groups[0].id]["method"] == "kit_links_connected_components"
    big = _cluster(_kit_corpus(40, chain=True, common=False), max_group_campaigns=10)
    assert big.groups and all(len(x.campaign_ids) <= 10 for x in big.groups)
    meta = big.group_meta[big.groups[0].id]
    assert meta["method"] in ("kit_links+leiden", "kit_links+louvain") and meta["split_from_campaigns"] == 40
    assert any("podział" in a for a in big.alerts)


def test_pair_sides_are_canonical_whatever_the_argument_order_audit_a01():
    """Audit reproduction `pair_evidence_side_swapped`."""
    from hydra.contracts import EvidencePointer

    def one(iid, art):
        return build_genome(iid, [Gene(incident_id=iid, family=Family.INF, kind="tracker_id", value="shared-123",
                                       dependency_group="INF:operator:tracker", extractor="t@1",
                                       evidence=EvidencePointer(artifact_id=art, bbox=(1, 2, 3, 4), har_entry=7,
                                                                timecode_s=1.5, char_range=(0, 5)))], {})

    early, late = one("a-known", "a" * 64), one("z-new", "b" * 64)
    rarity = build_rarity({"a-known": early.genes, "z-new": late.genes}, {})
    for x, y in ((late, early), (early, late)):
        s = score_pair(x, y, rarity).score
        r = next(m for m in s.reasons if m.kind == "tracker_id")
        assert (s.a, s.b) == ("a-known", "z-new")
        assert r.evidence_a.artifact_id == "a" * 64 and r.evidence_b.artifact_id == "b" * 64
    assert score_pair(late, early, rarity).score.model_dump() == score_pair(early, late, rarity).score.model_dump()


def test_asset_explanations_name_the_file_type():
    from hydra.contracts import EvidencePointer
    from hydra.synapse import asset_label_pl

    assert asset_label_pl("asset_sha256", EvidencePointer(note="libre_franklin-400.woff2")).endswith("czcionki strony")
    assert "CSS" in asset_label_pl("asset_sha256", EvidencePointer(note="main.css.pobrane"))
    assert "(JS)" in asset_label_pl("asset_sha256", EvidencePointer(dom_path="x_files/main.js"))
    assert asset_label_pl("asset_sha256", EvidencePointer(note="01-hero.webp")).endswith("graficzny strony")
    assert asset_label_pl("host", EvidencePointer(note="a.css")) is None


def test_stock_page_images_do_not_count_as_near_duplicates():
    from collections import defaultdict

    from hydra.entropy import build_rarity
    from hydra.synapse import _visual_near_duplicate

    rng = np.random.default_rng(6)  # five DIFFERENT images (copies inside one incident count once, CR-ASTRA-2)
    hashes = [f"{int(x):016x}" for x in rng.integers(0, 2**63, 5)]
    vectors = [np.eye(8, dtype=np.float32)[u] for u in range(5)]
    genes, vecs = {}, {}
    for k in range(6):  # the same 5 page images in 6 incidents = stock/template, df 6 > 2
        iid = f"i{k}"
        genes[iid] = []
        for u in range(5):
            gs, vs = _unit(iid, f"u{u}{iid}", "page_image", hashes[u], vectors[u])
            genes[iid] += gs
            vecs.update(vs)
    gen = {i: build_genome(i, gs, vecs) for i, gs in genes.items()}
    r = build_rarity(genes, {})
    assert _visual_near_duplicate(gen["i0"], gen["i1"], defaultdict(list), r) == 0
    assert _visual_near_duplicate(gen["i0"], gen["i1"], defaultdict(list), None) == 5
