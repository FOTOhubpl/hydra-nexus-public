"""Regression on the organiser sample (all registered extractors). Skipped when the sample is not present.

The test file holds no organiser data: only incident folder names and the expected grouping (acceptance from
the architect, 2026-11-21, consistent with analysis/infra INFRA_GENES.md and incidents/CAMPAIGNS.md umbrella):
campaigns {1,2,3} and {48,61} in one campaign group; 5, 10, 14, 36, 47 singletons; leave-one-out assignment
returns each member to its campaign and each singleton as a new pattern; a held-out campaign is never
assigned to its sibling campaign.

Slow on a cold cache (gene extraction ≈ 70 s on the M4 laptop); uses a private state dir under the pytest
tmp path, so it never touches ~/HydraNexus/state. Opt in with HYDRA_SAMPLE_TEST=1.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hydra.config import Settings

SAMPLE = Path(os.environ.get("HYDRA_SAMPLE_DIR", Path.home() / "HydraNexus" / "data" / "nask" / "sample"))
CAMPAIGNS = [{"nask-1", "nask-2", "nask-3"}, {"nask-48", "nask-61"}]
SINGLETONS = {"nask-5", "nask-10", "nask-14", "nask-36", "nask-47"}
KIT_SHARING = {"nask-1", "nask-2", "nask-48", "nask-61"}  # 3 links to {1,2} via its own landing, not the kit

pytestmark = pytest.mark.skipif(not SAMPLE.is_dir() or os.environ.get("HYDRA_SAMPLE_TEST") != "1",
                                reason="organiser sample not present or HYDRA_SAMPLE_TEST!=1")


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory):
    from hydra.config import get_settings
    from hydra.pipeline import analyse

    root = tmp_path_factory.mktemp("sample")
    base = get_settings()
    s = Settings(root=root, data_dir=base.data_dir, models_dir=base.models_dir, cache_dir=base.cache_dir,
                 state_dir=root / "state")
    s.ensure_dirs()
    return s, analyse([SAMPLE], s)


def test_campaigns_and_group(sample_run):
    _s, res = sample_run
    camps = [set(c.members) for c in res.clustering.campaigns]
    for want in CAMPAIGNS:
        assert want in camps
    for single in SINGLETONS:
        assert {single} in camps
    by_members = {frozenset(c.members): c for c in res.clustering.campaigns}
    a, b = (by_members[frozenset(x)] for x in CAMPAIGNS)
    assert a.group_id is not None and a.group_id == b.group_id
    group = next(g for g in res.clustering.groups if g.id == a.group_id)
    assert set(group.campaign_ids) == {a.id, b.id}


def test_hard_negatives_stay_apart(sample_run):
    _s, res = sample_run
    p = res.pairs
    assert p[("nask-10", "nask-3")].score.hypothesis != "same_campaign"  # TV brand / presenter candidate only
    assert p[("nask-14", "nask-47")].score.hypothesis != "same_campaign"  # at most group level (logo strip)


def test_partial_shared_audio_does_not_group_unrelated_incidents(sample_run):
    # nask-5 and nask-14 share one partial audio clip (containment 0.28) and nothing that identifies a kit; the
    # labels say they are unrelated, so they must not share a campaign group (architect review 2026-11-21).
    _s, res = sample_run
    camp = {m: c for c in res.clustering.campaigns for m in c.members}
    g5, g14 = camp["nask-5"].group_id, camp["nask-14"].group_id
    assert g5 is None or g5 != g14, (g5, g14)


def test_leave_one_out_and_held_out_campaign(sample_run):
    from hydra.assign import assign
    from hydra.pipeline import rarity_for

    _s, res = sample_run
    camps, groups, genomes = res.clustering.campaigns, res.clustering.groups, res.genomes
    member_of = {m: c.id for c in camps for m in c.members}
    for iid, g in genomes.items():
        rest = [c.model_copy(update={"members": [m for m in c.members if m != iid]}) for c in camps]
        rest = [c for c in rest if c.members]
        corpus = {k: v for k, v in genomes.items() if k != iid}
        r = rarity_for(genomes)
        r.kit_unit_of = {m: (c.group_id or c.id) for c in rest for m in c.members}
        a = assign(g, corpus, rest, groups, r).assignment
        if iid in SINGLETONS:
            assert a.decision == "new_campaign", (iid, a.reason_pl)
        else:
            assert a.decision == "assign" and a.campaign_id == member_of[iid], (iid, a.reason_pl)
    for held in camps:
        if len(held.members) < 2:
            continue
        rest = [c for c in camps if c.id != held.id]
        corpus = {k: v for k, v in genomes.items() if k not in held.members}
        for m in held.members:
            # EVAL-style caller: campaign-group units for every gene (assign() must normalise this itself)
            unit_of = {x: (c.group_id or c.id) for c in rest for x in c.members}
            for r in (rarity_for({**corpus, m: genomes[m]}, unit_of), None):
                a = assign(genomes[m], corpus, rest, groups, r).assignment
                assert a.decision == "new_campaign", (m, a.decision, a.reason_pl)
                if m in KIT_SHARING:  # shares the aio kit with the sibling campaign → same group
                    assert a.group_id == held.group_id is not None, (m, a.reason_pl)


def test_sample_creative_uploaded_as_screenshot_returns_to_its_campaign(sample_run):
    from hydra.pipeline import analyse_input

    s, res = sample_run
    creative = SAMPLE / "61" / "reklama_1.png"
    if not creative.is_file():
        pytest.skip("creative not in this sample")
    out = analyse_input("screenshot", creative, s)
    camp = next(c for c in res.clustering.campaigns if "nask-61" in c.members)
    a = out["assignment"]
    assert a["decision"] == "assign" and a["campaign_id"] == camp.id, a["reason_pl"]
    assert a["candidates"][0]["campaign_id"] == camp.id


def _only(g, fams, suffix=""):
    from hydra.synapse import build_genome

    vec = {x.vector_ref: (m, v) for (_k, m), (mat, gs) in g.vectors.items() for x, v in zip(gs, mat, strict=True)}
    iid = g.incident_id + suffix
    return build_genome(iid, [x.model_copy(update={"incident_id": iid}) for x in g.genes if str(x.family) in fams],
                        vec)


def test_single_family_runs_never_merge_campaigns(sample_run):
    from hydra.pipeline import score_and_cluster
    from hydra.synapse import Thresholds

    _s, res = sample_run
    truth = {m: c for c in CAMPAIGNS for m in c}
    for fams in ({"TXT"}, {"VIS"}, {"ID"}, {"NAR"}, {"TXT", "VIS", "ID", "NAR", "META"}):
        gen = {i: _only(g, fams) for i, g in res.genomes.items()}
        _r, _p, cl, _ = score_and_cluster(gen, Thresholds())
        for c in cl.campaigns:
            if len(c.members) > 1:
                same = all(truth.get(m) == truth.get(c.members[0]) and truth.get(m) for m in c.members)
                assert same, (fams, c.members)


def test_variants_without_infrastructure_return_to_their_campaign(sample_run):
    from hydra.assign import assign

    _s, res = sample_run
    camps, groups = res.clustering.campaigns, res.clustering.groups
    member = {m: c.id for c in camps for m in c.members}
    for iid in ("nask-1", "nask-2", "nask-48", "nask-61"):
        new = _only(res.genomes[iid], {"TXT", "VIS", "ID", "NAR", "META"}, "-variant")
        rest = [c.model_copy(update={"members": [m for m in c.members if m != iid]}) for c in camps]
        a = assign(new, {k: v for k, v in res.genomes.items() if k != iid}, [c for c in rest if c.members], groups,
                   None).assignment
        assert a.decision == "assign" and a.campaign_id == member[iid], (iid, a.reason_pl)
    for iid in SINGLETONS:
        new = _only(res.genomes[iid], {"TXT", "VIS", "ID", "NAR", "META"}, "-variant")
        a = assign(new, {k: v for k, v in res.genomes.items() if k != iid}, camps, groups, None).assignment
        assert a.decision != "assign", (iid, a.reason_pl)
