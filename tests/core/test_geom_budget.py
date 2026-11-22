"""Geometry budget is explicit (Astra review of d5d5c3b, 2026-11-22): skips are counted, logged and reported."""
from hydra import vis_geom


def test_default_budget_is_large():
    assert vis_geom.BUDGET > 0  # default 100 000, HYDRA_GEOM_BUDGET overrides


def test_exhausted_budget_is_counted_and_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(vis_geom, "BUDGET", 0)
    monkeypatch.setattr(vis_geom, "_SPENT", [0])
    monkeypatch.setattr(vis_geom, "SKIPPED", [0])
    monkeypatch.setattr(vis_geom, "_WARNED", [False])
    monkeypatch.setattr(vis_geom, "_MEMO", {})
    monkeypatch.setattr(vis_geom, "_locate", lambda sha, settings=None: tmp_path)
    assert vis_geom.cached_verify(("a" * 64, None), ("b" * 64, None)) is None
    s = vis_geom.stats()
    assert s["skipped_budget"] == 1 and s["complete"] is False and s["budget"] == 0
