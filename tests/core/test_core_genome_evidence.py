"""B10: campaign core genome entries carry evidence pointers (synthetic corpus, no organiser data)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from hydra.config import Settings
from hydra.pipeline import analyse
from tests.core.test_pipeline import corpus, test_extractors  # noqa: F401 — fixture


def test_core_genome_has_evidence_of_two_holders(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    from hydra.api import create_app

    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    camp = next(c for c in res.clustering.campaigns if c.members == ["nask-1", "nask-2", "nask-3"])
    assert camp.core_genome
    for g in camp.core_genome:
        assert g.evidence_a is not None and g.evidence_a.artifact_id, g
        assert g.evidence_b is not None and g.evidence_b.artifact_id, g
        assert g.evidence_a.artifact_id != g.evidence_b.artifact_id  # two different members' pages
    with TestClient(create_app(settings)) as c:
        card = c.get(f"/v1/campaigns/{camp.id}").json()
    assert card["core_genome"] and all(g["evidence_a"]["artifact_id"] for g in card["core_genome"])
