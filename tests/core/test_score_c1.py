"""SCORE-C1 / C2 (2026-11-22): df regime parity between analyse clustering and assignment, and the
leave-one-out self-assignment cache. Synthetic genes only."""

from __future__ import annotations

from hydra.entropy import build_postings, operator_seed_units
from hydra.pipeline import score_and_cluster
from hydra.synapse import Thresholds, build_genome, score_pair
from tests.adversarial.helpers import gene

N = 20


def _genomes(rows):
    rows = dict(rows)
    for k in range(N - len(rows)):
        iid = f"bg{k:02d}"
        rows[iid] = [gene(iid, "host", f"{iid}.invalid", "INF:operator:host")]
    return {i: build_genome(i, gs, {}) for i, gs in rows.items()}, rows


def test_five_member_campaign_sharing_one_operator_identity_is_one_campaign():
    """Finding C1: one shared tracker in n=20 gave k=5 members 2.66 bits per pair under the incident df (five
    singletons); with pass-0 operator units the df regime is the assignment one and the campaign is recovered."""
    rows = {f"m{k}": [gene(f"m{k}", "tracker_id", "synthetic-op-c1", "INF:operator:tracker"),
                      gene(f"m{k}", "host", f"m{k}.invalid", "INF:operator:host")] for k in range(5)}
    genomes, raw = _genomes(rows)
    units = operator_seed_units(raw)
    assert units is not None and len({units[f"m{k}"] for k in range(5)}) == 1 and "bg00" not in units
    rarity, _p, cl, _ = score_and_cluster(genomes, Thresholds())
    assert len({cl.membership[f"m{k}"] for k in range(5)}) == 1
    assert sum(1 for c in cl.campaigns if set(c.members) & set(rows)) == 1
    pe = score_pair(genomes["m0"], genomes["m3"], rarity)
    assert pe.score.hypothesis == "same_campaign"
    # background incidents stay singletons
    assert all(len(c.members) == 1 for c in cl.campaigns if not set(c.members) & set(rows))


def test_five_incidents_sharing_a_platform_value_stay_separate():
    """Negative control: a CDN / platform value (platform tier, also on an account-kind host) shared by five
    unrelated incidents does not form units and does not group them."""
    rows = {f"u{k}": [gene(f"u{k}", "host", "cdn.synthetic-cdn.invalid", "INF:platform:cdn"),
                      gene(f"u{k}", "landing_host", "shop.synthetic-platform.invalid", "INF:platform:shop_platform"),
                      gene(f"u{k}", "tracker_id", f"synthetic-own-{k}", "INF:operator:tracker")] for k in range(5)}
    genomes, raw = _genomes(rows)
    assert operator_seed_units(raw) is None
    _r, _p, cl, _ = score_and_cluster(genomes, Thresholds())
    assert len({cl.membership[f"u{k}"] for k in range(5)}) == 5


def test_operator_value_above_hub_limit_forms_no_unit():
    """An operator value held by more than max(5, 20 % of incidents) is a hub: no pass-0 unit."""
    rows = {f"h{k}": [gene(f"h{k}", "tracker_id", "synthetic-hub", "INF:operator:tracker")] for k in range(6)}
    _g, raw = _genomes(rows)
    assert operator_seed_units(raw) is None
    assert len(build_postings(raw)[("tracker_id", "synthetic-hub")]) == 6
