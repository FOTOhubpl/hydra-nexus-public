"""CR-ASTRA-SW-01 / SW-02 (Astra audit of f450a70, 2026-11-22): geometry verification states.

Reproducers moved from the audit bundle (astra-software-evidence-a370d4d/test_confirmed.py) with unchanged
assertions, plus regression checks of the fix. Synthetic images only; tests/conftest.py isolation stays active.
"""
from __future__ import annotations

import hashlib
import io

import pytest

from hydra import vis_geom
from hydra.storage import ContentStore


@pytest.fixture
def geometry_state(monkeypatch):
    for key, value in (("_MEMO", {}), ("_SPENT", [0]), ("SKIPPED", [0]), ("_WARNED", [False])):
        monkeypatch.setattr(vis_geom, key, value)
    monkeypatch.setenv("HYDRA_ADV_CROP", "1")
    monkeypatch.setenv("HYDRA_ADV_GEOM", "0")
    monkeypatch.setenv("HYDRA_ADV_HSV", "0")


def test_sw01_budget_exhaustion_does_not_change_complete_pair_decision(
    geometry_state, monkeypatch, tmp_path,
):
    from tests.core.test_adv_vis import _creative, _crop, _pair

    monkeypatch.setattr(vis_geom, "_locate", lambda artifact_id, settings=None: tmp_path)
    original = _creative(5)
    crop = _crop(original, fx=(0.12, 0.0, 0.0, 0.12))
    before = _pair(crop, original, monkeypatch=monkeypatch)
    assert before.score.hypothesis == "same_campaign"
    vis_geom._MEMO.clear()
    for path in (tmp_path / "work" / "vis_geom").glob("*.json"):
        path.unlink()
    vis_geom._SPENT[0] = vis_geom.BUDGET
    after = _pair(crop, original, monkeypatch=monkeypatch)
    print({"before_bits": before.campaign_bits, "after_bits": after.campaign_bits,
           "before": before.score.hypothesis, "after": after.score.hypothesis, "stats": vis_geom.stats()})
    # A resource refusal must preserve the decision or produce explicit pair-level incompleteness/abstention.
    assert after.score.hypothesis != "independent"


def test_sw01_skipped_verification_is_marked_on_the_pair(geometry_state, monkeypatch, tmp_path):
    """The abstention is explicit: a 0-bit Polish note and `meta.geometry_incomplete`, counted in stats."""
    from tests.core.test_adv_vis import _creative, _crop, _pair

    monkeypatch.setattr(vis_geom, "_locate", lambda artifact_id, settings=None: tmp_path)
    original = _creative(5)
    vis_geom._SPENT[0] = vis_geom.BUDGET
    pe = _pair(_crop(original, fx=(0.12, 0.0, 0.0, 0.12)), original, monkeypatch=monkeypatch)
    assert pe.score.hypothesis == "abstain"
    assert pe.score.meta.get("geometry_incomplete") == 1
    notes = [r for r in pe.score.reasons if r.kind == vis_geom.SKIPPED_KIND]
    assert len(notes) == 1 and notes[0].bits == 0.0
    assert "pominięta" in notes[0].explanation_pl and "niepełny" in notes[0].explanation_pl
    assert vis_geom.stats()["complete"] is False and vis_geom.stats()["skipped_budget"] >= 1


def test_sw01_verified_negative_stays_independent(geometry_state, monkeypatch, tmp_path):
    """No shared features after a verification that DID run is ordinary negative evidence, not an abstention."""
    from tests.core.test_adv_vis import _creative, _pair

    monkeypatch.setattr(vis_geom, "_locate", lambda artifact_id, settings=None: tmp_path)
    pe = _pair(_creative(5), _creative(77), monkeypatch=monkeypatch)
    assert pe.score.hypothesis == "independent"
    assert "geometry_incomplete" not in pe.score.meta
    assert vis_geom.stats()["complete"] is True


def test_sw01_missing_image_is_no_evidence_not_incomplete(geometry_state, monkeypatch, tmp_path):
    """An artefact that is not available is 'no observation' (unknown), not a skipped verification."""
    from tests.core.test_adv_vis import _creative, _crop, _pair

    monkeypatch.setattr(vis_geom, "_locate", lambda artifact_id, settings=None: None)
    original = _creative(5)
    pe = _pair(_crop(original, fx=(0.12, 0.0, 0.0, 0.12)), original)
    assert "geometry_incomplete" not in pe.score.meta
    assert vis_geom.stats()["skipped_budget"] == 0


def _image_bytes(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_sw02_missing_artifact_is_retried_after_it_is_imported(geometry_state, settings):
    from tests.core.test_adv_vis import _creative, _crop

    original = _creative(1)
    a, b = _image_bytes(_crop(original)), _image_bytes(original)
    ra, rb = (hashlib.sha256(a).hexdigest(), None), (hashlib.sha256(b).hexdigest(), None)
    assert vis_geom.cached_verify(ra, rb, settings) is None
    cas = ContentStore(settings.cas_dir)
    assert cas.put_bytes(a) == ra[0]
    assert cas.put_bytes(b) == rb[0]
    after_import = vis_geom.cached_verify(ra, rb, settings)
    vis_geom._MEMO.clear()  # Control: same inputs, no stale negative entry.
    control = vis_geom.cached_verify(ra, rb, settings)
    assert control and control["crop_match"], control
    print({"after_import": after_import, "after_clear_crop_match": control["crop_match"]})
    assert after_import and after_import["crop_match"]


def test_sw02_missing_artifact_does_not_spend_budget_and_measurements_stay_cached(
    geometry_state, settings, monkeypatch,
):
    from tests.core.test_adv_vis import _creative, _crop

    original = _creative(1)
    a, b = _image_bytes(_crop(original)), _image_bytes(original)
    ra, rb = (hashlib.sha256(a).hexdigest(), None), (hashlib.sha256(b).hexdigest(), None)
    cas = ContentStore(settings.cas_dir)
    cas.put_bytes(a)  # only one side present: the pair cannot be measured yet
    assert vis_geom.cached_verify(ra, rb, settings) is None
    assert vis_geom._SPENT[0] == 0 and not vis_geom._MEMO
    cas.put_bytes(b)
    first = vis_geom.cached_verify(ra, rb, settings)
    assert first and first["crop_match"] and vis_geom._SPENT[0] == 1
    monkeypatch.setattr(vis_geom, "verify", lambda *x, **k: pytest.fail("a real measurement must stay cached"))
    assert vis_geom.cached_verify(ra, rb, settings) is first
