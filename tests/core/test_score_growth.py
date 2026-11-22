"""SCORE-GROWTH (ASTRA-DELTA-07, CR-SCORE-GROWTH-1, 2026-11-22): growth behaviour of pass-0 operator units in
analyse. Default mode keeps the SCORE-C1 hard limit (the cliff is reproduced and pinned here); the experimental
HYDRA_OPERATOR_UNITS=smooth mode grades the one-unit regime instead. Synthetic genes only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from hydra.entropy import operator_seed_soft, operator_units_mode

_spec = importlib.util.spec_from_file_location(
    "score_growth_grid", Path(__file__).resolve().parents[2] / "scripts" / "score_growth_grid.py")
grid = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grid)

ASTRA_CELLS = [(5, 15), (6, 15), (6, 20)]  # (members, background) = totals 20 / 21 / 26 of ASTRA-DELTA-07


@pytest.fixture
def smooth(monkeypatch):
    monkeypatch.delenv("HYDRA_OPERATOR_UNITS", raising=False)  # smooth is the default
    assert operator_units_mode() == "smooth"


def test_legacy_mode_reproduces_the_astra_cliff(monkeypatch):
    """Default (frozen scoring): 5 members in n=20 are one campaign, 6 in n=21 split into 6, 6 in n=26 are one."""
    monkeypatch.setenv("HYDRA_OPERATOR_UNITS", "legacy")
    rows = [grid.probe(m, b) for m, b in ASTRA_CELLS]
    assert [r["campaigns_among_members"] for r in rows] == [1, 6, 1]
    assert [r["pair_hypothesis"] for r in rows] == ["same_campaign", "independent", "same_campaign"]
    assert rows[0]["pair_bits"] - rows[1]["pair_bits"] > 10


def test_legacy_mode_links_operators_through_a_vendor_tracker(monkeypatch):
    """Default mode: a vendor tracker held by 3 operators x 2 members (n=26) is under the seed limit and unions
    the three operators (pinned so a default change is visible; the smooth mode keeps them apart)."""
    monkeypatch.setenv("HYDRA_OPERATOR_UNITS", "legacy")
    assert not grid.vendor(3, 2, 20)["non_linking"]


def test_smooth_mode_has_no_cliff_on_the_astra_cells(smooth):
    rows = [grid.probe(m, b) for m, b in ASTRA_CELLS]
    assert all(r["campaigns_among_members"] == 1 for r in rows)
    assert all(r["pair_hypothesis"] == "same_campaign" for r in rows)
    assert abs(rows[0]["pair_bits"] - rows[1]["pair_bits"]) < 2.5


def test_smooth_mode_is_graded_and_monotone(smooth):
    """members 4..12 x background 10..30 (step 5): adjacent cells differ by at most 4 bits, bits never rise with
    members and never fall with background."""
    cells = [grid.probe(m, b) for m in range(4, 13) for b in range(10, 31, 5)]
    stats = grid.cliff_stats([dict(r) for r in cells])
    assert stats["wrong_direction_steps"] == 0
    # background step of 5: compare members steps only (one holder at a time)
    by = {(r["members"], r["background"]): r["pair_bits"] for r in cells}
    assert max(abs(by[(m + 1, b)] - by[(m, b)]) for m in range(4, 12) for b in range(10, 31, 5)) <= 4.0


@pytest.mark.parametrize("ops,per_op,bg", [(3, 2, 0), (3, 2, 20), (3, 3, 40), (4, 2, 40), (6, 3, 20)])
def test_smooth_mode_vendor_tracker_stays_non_linking(smooth, ops, per_op, bg):
    r = grid.vendor(ops, per_op, bg)
    assert r["non_linking"], r
    assert r["within_hypothesis"] == "same_campaign"


def test_smooth_mode_chain_and_two_identifier_growth(smooth):
    assert all(grid.chain(m, b)["campaigns_among_members"] == 1 for m in (3, 10) for b in (0, 40))
    two = ("entity_phone", "+48500999000", "INF:operator:phone")
    assert all(grid.probe(m, 20, (two,))["campaigns_among_members"] == 1 for m in (6, 10, 15))


def test_smooth_seed_function_spread_and_weights():
    from tests.adversarial.helpers import gene

    raw = {}
    for o in range(3):
        for k in range(2):
            iid = f"o{o}m{k}"
            raw[iid] = [gene(iid, "tracker_id", "vendor", "INF:operator:tracker"),
                        gene(iid, "entity_phone", f"+4850000{o}", "INF:operator:phone")]
    for k in range(7):
        raw[f"t{k}"] = [gene(f"t{k}", "tracker_id", "grow", "INF:operator:tracker")]
    for k in range(20):
        raw[f"bg{k}"] = [gene(f"bg{k}", "host", f"bg{k}.invalid", "INF:operator:host")]
    units, soft, spread = operator_seed_soft(raw)
    assert ("tracker_id", "vendor") in spread
    assert units is not None and len({units[f"o{o}m0"] for o in range(3)}) == 3
    # n = 33: L = 7, U = 17 -> the 7-holder value is a full unit, not soft
    assert units["t0"] == units["t6"] and (soft is None or ("tracker_id", "grow") not in soft)


def test_mode_parsing(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_UNITS", "bogus")
    assert operator_units_mode() == "smooth"
    monkeypatch.setenv("HYDRA_OPERATOR_UNITS", " Legacy ")
    assert operator_units_mode() == "legacy"
    monkeypatch.delenv("HYDRA_OPERATOR_UNITS", raising=False)
    assert operator_units_mode() == "smooth"
