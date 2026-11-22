"""EVAL-EXTRA measurement helpers on a tiny synthetic fixture (no state, no organiser data)."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from eval import calibration as C
from eval import family_contrib as F
from eval import threshold_sweep as T


def _pairs(n_camp: int = 6, per: int = 4, seed: int = 0) -> list[dict]:
    """Synthetic pairs: same-campaign pairs get high support bits, cross-campaign pairs low (with overlap)."""
    rng = random.Random(seed)
    ids = [(f"c{c}", f"i{c}-{k}") for c in range(n_camp) for k in range(per)]
    rows = []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            same = ids[x][0] == ids[y][0]
            s = rng.uniform(8, 20) if same else rng.uniform(0, 11)
            rows.append({"label_a": ids[x][0], "label_b": ids[y][0], "support": s,
                         "probability": 1 / (1 + 2 ** (-(s - 10)))})
    return rows


def test_brier_and_ece_extremes():
    assert C.brier([1.0, 0.0], [1, 0]) == 0.0
    assert C.brier([0.0, 1.0], [1, 0]) == 1.0
    assert C.ece([0.95, 0.05], [1, 0]) == pytest.approx(0.05)
    bins = C.reliability_bins([0.0, 1.0, 0.55], [0, 1, 1], bins=10)
    assert [b["n"] for b in bins] == [1, 0, 0, 0, 0, 1, 0, 0, 0, 1]  # 1.0 falls in the last bin


def test_campaign_split_is_disjoint_and_deterministic():
    labels = [f"c{i}" for i in range(7)] * 3
    a, b = C.campaign_split(labels, 3)
    assert a.isdisjoint(b) and a | b == set(labels)
    assert C.campaign_split(labels, 3) == (a, b)


def test_isotonic_split_beats_nothing_and_is_monotone():
    res = C.run(_pairs(), splits=3)
    assert res["splits_used"] == 3
    s = res["per_split"][0]
    assert s["fit_pairs"] and s["test_pairs"]
    ys = [y for _x, y in s["map"]]
    assert ys == sorted(ys)
    assert 0.0 <= s["isotonic"]["brier"] <= 0.25
    assert res["note_pl"] == "kalibracja wyświetlana, decyzje nadal na bitach"


def test_split_without_both_classes_is_skipped():
    rows = [{"label_a": "a", "label_b": "a", "support": 12.0, "probability": 0.8}] * 4
    assert C.evaluate_split(rows, 0) is None


def test_chart_writes_png(tmp_path):
    png = tmp_path / "r.png"
    C.chart(C.run(_pairs(), splits=1), png, "test")
    assert png.stat().st_size > 1000


def test_sim_truth_inherits_source_label_and_negatives_are_singletons():
    labels = {"nask-1": {"label": "A"}, "nask-2": {"label": "B", "uncertain": True}}
    rows = [{"item": "p1", "expect": "known:nask-1", "variant": "zip", "source": "nask-1"},
            {"item": "p2", "expect": "known:nask-2", "variant": "zip", "source": "nask-2"},
            {"item": "n1", "expect": "new", "variant": "tpl_clone", "source": "template:nask-1"},
            {"item": "d1", "expect": "known:nask-1", "variant": "dup_exact", "source": "nask-1"}]
    inc = {"p1": "new-1", "p2": "new-2", "n1": "new-3", "d1": "nask-1"}
    truth, variant = T.sim_truth(rows, inc, labels)
    assert truth == {"nask-1": "A", "new-1": "A", "new-3": "neg:n1"}
    assert variant["nask-1"] == "sample" and variant["new-2"] == "zip"


def test_cross_source_counts_certain_false_merges():
    pred = {"a": "C1", "b": "C1", "c": "C1", "d": "C2", "e": "C3"}
    group = {"a": "nask", "b": "phreshphish", "c": "phreshphish", "d": "nask", "e": "purino-scam-ads"}
    r = T.cross_source(pred, group)
    assert r["cross_source_campaigns"] == 1 and r["cross_source_pairs"] == 2
    assert r["largest_campaign"] == 3 and r["campaigns"] == 3
    assert T.source_group("ext:phreshphish-2k") == T.source_group("ext:phreshphish") == "phreshphish"


def test_truth_metrics_and_pair_rows():
    m = T.truth_metrics({"a": "x", "b": "x", "c": "x"}, {"a": "A", "b": "A", "c": "B"})
    assert m["false_merge_pairs"] == 2 and m["impure_campaigns"] == 1
    pe = SimpleNamespace(support=11.0, score=SimpleNamespace(total_bits=12.0, probability=0.6,
                                                              hypothesis="same_campaign"))
    rows = T.pair_rows({("a", "b"): pe, ("a", "z"): pe}, {"a": "A", "b": "A"})
    assert len(rows) == 1 and rows[0]["label_a"] == rows[0]["label_b"] and "a" not in rows[0].values()


def test_family_summary():
    judged = {"all": [{"variant": "zip", "expect": "known:x", "outcome": "ok"},
                      {"variant": "tpl_clone", "expect": "new", "outcome": "ok_new"}],
              "-TXT": [{"variant": "zip", "expect": "known:x", "outcome": "missed_new"},
                       {"variant": "tpl_clone", "expect": "new", "outcome": "false_assign"}]}
    s = F.summarise(judged)
    assert s["all"]["assign_accuracy"] == 1.0 and s["all"]["negatives_rejected"] == 1
    assert s["-TXT"]["false_assigns"] == 1 and s["-TXT"]["per_variant"]["zip"]["missed_new"] == 1


def test_variant_diagnostics():
    pred = {"s1": "C1", "v1": "C1", "v2": "C2", "n1": "C1"}
    truth = {"s1": "A", "v1": "A", "v2": "A", "n1": "neg:n1"}
    variant = {"s1": "sample", "v1": "zip", "v2": "creative1", "n1": "tpl_clone"}
    d = T.variant_diagnostics(pred, truth, variant)
    assert d["positives_with_source"] == {"creative1": {"n": 1, "with_source": 0}, "zip": {"n": 1, "with_source": 1}}
    assert d["impure_mix"] == [{"sample": 1, "tpl_clone": 1, "zip": 1}]
