"""KONTRA fixtures: the synthetic spine corpus of tests/core/test_pipeline.py (no organiser data), temp state."""

from __future__ import annotations

import pytest

from hydra.contracts import Family
from hydra.genome import base
from tests.core.test_pipeline import PX, corpus, page, t_page, t_vec


@pytest.fixture
def kontra_extractors():
    added = []
    for name, fn, fam in (("t_page", t_page, Family.INF), ("t_vec", t_vec, Family.VIS)):
        if name not in base.REGISTRY:
            base.register(name, family=fam)(fn)
            added.append(name)
    yield ["t_page", "t_vec"]
    for name in added:
        base.REGISTRY.pop(name, None)


@pytest.fixture
def kontra_state(settings, make_png, tmp_path, kontra_extractors):
    """Analysed synthetic corpus + three assigned items: `assign` (campaign A), `kit` (new pattern, group of A/B
    via the shared SDK), `none` (new pattern without a group)."""
    from hydra.pipeline import analyse, assign_new

    analyse([corpus(tmp_path, make_png)], settings, only=kontra_extractors)
    page(tmp_path / "new_a", "evil-a.example", "+48 700 100 100" + PX)
    _, a = assign_new(tmp_path / "new_a", settings, only=kontra_extractors)
    page(tmp_path / "new_kit", "fresh.example", '<i data-sdk="aio-sdk-1.0.28-x"></i>')
    _, k = assign_new(tmp_path / "new_kit", settings, only=kontra_extractors)
    page(tmp_path / "new_none", "other.example")
    _, n = assign_new(tmp_path / "new_none", settings, only=kontra_extractors)
    return {"settings": settings, "assign": a, "kit": k, "none": n}
