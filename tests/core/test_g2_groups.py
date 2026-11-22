"""G2 campaign-group acceptance (SYNTHETIC fixtures): HYDRA_ADV_GROUPS=1 confirmed group bridges.

Campaign partitions must be identical with the flag on and off; only the group layer may change."""

from __future__ import annotations

from itertools import combinations

import pytest

from hydra.contracts import Family, Gene
from hydra.entropy import build_rarity
from hydra.nexus import cluster
from hydra.synapse import build_genome, level, score_pair


def g(iid, kind, value, group=None, fam=Family.INF):
    return Gene(incident_id=iid, family=fam, kind=kind, value=value, extractor="t@1", dependency_group=group)


def operator(iid, op):
    """Operator identity of campaign `op`: entry host, tracker, lead endpoint, account (independent operator genes)."""
    return [g(iid, "host", f"{op}.example", "INF:operator:host"),
            g(iid, "tracker_id", f"UA-{op}", "INF:operator:tracker"),
            g(iid, "lead_endpoint", f"https://{op}.example/lead", "INF:operator:lead"),
            g(iid, "operator_id", f"acct-{op}", "INF:operator:account")]


def kit(iid, k):
    """Identity kit `k`: two independent kit genes (CSS and JS build of one phishing kit)."""
    return [g(iid, "css_build_hash", f"css-{k}", "INF:kit:css_build"),
            g(iid, "js_build_hash", f"js-{k}", "INF:kit:js_build")]


def run(genes, monkeypatch, flag):
    monkeypatch.setenv("HYDRA_ADV_GROUPS", "1" if flag else "0")
    genes = dict(genes)
    for k in range(60):  # background with unique values (rarity)
        genes[f"bg{k:02d}"] = [g(f"bg{k:02d}", "host", f"bg{k}.example", "INF:operator:host")]
    r = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    pairs = {(a, b): score_pair(gen[a], gen[b], r) for a, b in combinations(sorted(gen), 2)}
    return cluster(gen, pairs, r)


def partition(cl):
    return sorted(sorted(c.members) for c in cl.campaigns)


def grouped(cl):
    return [sorted(m for cid in gr.campaign_ids for c in cl.campaigns if c.id == cid for m in c.members)
            for gr in cl.groups]


def both(genes, monkeypatch):
    off, on = run(genes, monkeypatch, False), run(genes, monkeypatch, True)
    assert partition(off) == partition(on)  # groups are a separate layer
    return off, on


def test_same_cms_cdn_stock_creates_no_campaign_and_no_group(monkeypatch):
    genes = {}
    for k in range(6):
        iid = f"x{k}"
        genes[iid] = [g(iid, "host", f"x{k}.example", "INF:operator:host"),
                      g(iid, "cms", "wordpress", "INF:platform:cms"),
                      g(iid, "cdn_host", "cdn.jsdelivr.net", "INF:platform:cdn"),
                      g(iid, "lib", "jquery-3.6.0", "INF:platform:libs"),
                      g(iid, "phash", "f" * 16, "vis_unit:stock", Family.VIS)]
    off, on = both(genes, monkeypatch)
    assert all(len(c.members) == 1 for c in on.campaigns)
    assert on.groups == [] and off.groups == []


def test_two_operators_with_the_same_kit_stay_separate_campaigns_kit_group_shows_kit_reason(monkeypatch):
    genes = {}
    for op in ("opa", "opb"):
        for k in range(3):
            iid = f"{op}{k}"
            genes[iid] = operator(iid, op) + kit(iid, "k1")
    off, on = both(genes, monkeypatch)
    assert sorted(len(c.members) for c in on.campaigns if not c.id.startswith("bg"))[-2:] == [3, 3]
    assert ["opa0", "opb0"] not in [[m for m in c.members if m.endswith("0")] for c in on.campaigns]
    assert len(on.groups) == 1 and len(on.groups[0].campaign_ids) == 2
    gr = on.groups[0]
    kit_reasons = [m for m in gr.shared_genes if m.kind in ("css_build_hash", "js_build_hash") and m.bits > 0]
    assert kit_reasons, "a kit group must show its kit-tier reason"
    assert all(level(g("t", m.kind, "v", f"INF:kit:{m.kind[:-5]}")) == "kit" for m in kit_reasons)
    assert "members of each campaign" in on.group_meta[gr.id]["rule"]
    assert grouped(off) == grouped(on)  # a real, confirmed kit group is kept


def test_two_weak_unrelated_traces_do_not_chain_a_b_c(monkeypatch):
    """A-B share one rare kit trace, B-C share another one: no A-B-C group (and no A-B or B-C group)."""
    genes = {"a": [g("a", "host", "a.example", "INF:operator:host"),
                   g("a", "css_build_hash", "css-ab", "INF:kit:css_build")],
             "b": [g("b", "host", "b.example", "INF:operator:host"),
                   g("b", "css_build_hash", "css-ab", "INF:kit:css_build"),
                   g("b", "js_build_hash", "js-bc", "INF:kit:js_build")],
             "c": [g("c", "host", "c.example", "INF:operator:host"),
                   g("c", "js_build_hash", "js-bc", "INF:kit:js_build")]}
    off, on = both(genes, monkeypatch)
    assert not any({"a", "b", "c"} <= set(m) for m in grouped(on))
    assert on.groups == []
    cand = {c.members[0]: c.meta.get("group_candidates") for c in on.campaigns if c.members[0] in "abc"}
    assert cand["b"] and {r["reason"] for r in cand["b"]} == {"single_kit_trace"}


def test_one_incident_with_a_foreign_artefact_does_not_bridge_two_campaigns(monkeypatch):
    """The 231-corpus mechanism: a member of campaign A carries campaign B's HAR (kit + hosts of B)."""
    genes = {}
    for op, k in (("opa", "ka"), ("opb", "kb")):
        for j in range(4):
            iid = f"{op}{j}"
            genes[iid] = operator(iid, op) + kit(iid, k)
    genes["opa3"] += [x.model_copy(update={"incident_id": "opa3"}) for x in kit("opa3", "kb")]
    off, on = both(genes, monkeypatch)
    assert len(off.groups) == 1  # flag off: the misfiled artefact merges the two campaigns' groups
    assert on.groups == []
    cand = [c.meta.get("group_candidates") for c in on.campaigns if "opa0" in c.members][0]
    assert cand and cand[0]["reason"] == "unconfirmed_bridge" and cand[0]["members_here"] == 1


@pytest.mark.parametrize("flag", [False, True])
def test_real_campaign_with_variants_still_merges(monkeypatch, flag):
    genes = {}
    for j in range(5):
        iid = f"v{j}"
        genes[iid] = operator(iid, "real")
        if j % 2:  # variants: some lose the lead endpoint and carry the kit
            genes[iid] = [x for x in genes[iid] if x.kind != "lead_endpoint"] + kit(iid, "kr")
    cl = run(genes, monkeypatch, flag)
    assert ["v0", "v1", "v2", "v3", "v4"] in partition(cl)
