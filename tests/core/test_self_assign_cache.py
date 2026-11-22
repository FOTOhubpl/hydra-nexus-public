"""SCORE-C1 finding C2 (2026-11-22): leave-one-out self-assignment must not reuse near-hash / MinHash
frequencies cached for another held-out incident. Synthetic genes only."""

from __future__ import annotations

import hydra.pipeline as pipeline
from hydra.contracts import Campaign
from hydra.synapse import Thresholds
from tests.adversarial.helpers import corpus, gene, image_unit


def test_self_assign_item_does_not_reuse_near_df_of_another_item(monkeypatch):
    """Finding C2: `_self_assign_item` shallow-copies the rarity; the near-hash cache keyed by the number of
    operator-unit members returned LOO(d) = 3 after LOO(a) (expected 2)."""
    h = "95aabbccddeeff17"
    rows, vectors = {}, {}
    for iid in ("a", "b", "c", "d"):
        rows[iid], refs = image_unit(iid, f"{iid}-ad", h)
        vectors.update(refs)
    rows["e"] = [gene("e", "host", "e.invalid", "INF:operator:host")]
    genomes, rarity = corpus(rows, vectors, background=0)
    camps = [Campaign(id="C1", label="C1", members=["a", "b", "c"]), Campaign(id="C2", label="C2", members=["d", "e"])]

    def fake_assign(new, corpus_, rest, groups, r, th, **_kw):
        if r.operator_unit_of is None:
            r.operator_unit_of = {m: c.id for c in rest for m in c.members}
        return r.near_df("phash", h, 10)

    monkeypatch.setattr(pipeline, "assign_open_set", fake_assign)
    assert rarity.near_df("phash", h, 10) == 4  # analyse scoring fills the parent's cache before self-assignment
    shared = (genomes, camps, [], rarity, Thresholds(), None, None, {})
    assert pipeline._self_assign_item(shared, "a") == 3  # a, C1 (b, c), C2 (d)
    assert pipeline._self_assign_item(shared, "d") == 2  # C1 (a, b, c), d
