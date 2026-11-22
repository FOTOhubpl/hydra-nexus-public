"""CR-ASTRA-SW-06: siblings of an absent parent / duplicate_of stay on one side of the eval split.

The first test is the Astra audit reproducer (astra-software-evidence-a370d4d/test_confirmed.py), assertion
unchanged. Synthetic rows only; this is a contract test of eval.run.campaign_split, not a claim about any
published metric.
"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("origin_key", ["parent", "duplicate_of"])
def test_sw06_split_keeps_siblings_of_external_origin_together(origin_key):
    from eval.run import campaign_split

    rows = [
        {"id": "child-a", "label": None, origin_key: "external-origin"},
        {"id": "child-b", "label": None, origin_key: "external-origin"},
    ]
    calibration, test, info = campaign_split(rows)
    origins_calib = {row[origin_key] for row in calibration}
    origins_test = {row[origin_key] for row in test}
    assert not origins_calib & origins_test


ZERO = {"parent_or_origin": 0, "campaign_label": 0, "duplicate": 0}


def _side(rows_a, rows_b, rid):
    return "a" if rid in {r["id"] for r in rows_a} else "b" if rid in {r["id"] for r in rows_b} else None


@pytest.mark.parametrize("origin_key", ["parent", "duplicate_of"])
def test_sw06_derivatives_only_subset_has_zero_intersections(origin_key):
    """A subset holding only derivatives of several absent origins: every origin family stays whole."""
    from eval.run import campaign_split

    rows = [{"id": f"o{o}-d{d}", "label": None, origin_key: f"absent-{o}"} for o in range(5) for d in range(3)]
    a, b, info = campaign_split(rows)
    assert info["intersections"] == ZERO
    assert len(a) + len(b) == len(rows)
    for o in range(5):
        assert len({_side(a, b, f"o{o}-d{d}") for d in range(3)}) == 1
    assert a and b  # five families still split into both sides
    assert campaign_split(list(reversed(rows)))[2] == info  # deterministic under input order


def test_sw06_mixed_present_parent_and_absent_origin_siblings():
    from eval.run import campaign_split

    rows = [
        {"id": "p1", "label": "kampania-1"},
        {"id": "p1-crop", "label": "kampania-1", "parent": "p1"},
        {"id": "p2", "label": "kampania-2"},
        {"id": "p2-dup", "label": None, "duplicate_of": "p2"},
        {"id": "x-a", "label": None, "parent": "gone"},
        {"id": "x-b", "label": "kampania-3", "parent": "gone"},
        {"id": "x-c", "label": None, "duplicate_of": "gone"},
        {"id": "y-a", "label": None, "duplicate_of": "gone-2"},
        {"id": "y-b", "label": None, "duplicate_of": "gone-2"},
        {"id": "single", "label": None},
    ]
    a, b, info = campaign_split(rows)
    assert info["intersections"] == ZERO
    assert len(a) + len(b) == len(rows)
    for family in (("p1", "p1-crop"), ("p2", "p2-dup"), ("x-a", "x-b", "x-c"), ("y-a", "y-b")):
        assert len({_side(a, b, rid) for rid in family}) == 1, family
    assert campaign_split(rows) == (a, b, info)
