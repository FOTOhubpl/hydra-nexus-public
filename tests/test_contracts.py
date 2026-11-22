"""Contracts v2 and migration v2 (architect-owned)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hydra import contracts as c
from hydra.db import MIGRATIONS, Writer, connect, schema_version


def test_migration_v2_creates_task_tables(tmp_path):
    w = Writer(tmp_path / "h.db")
    w.close()
    conn = connect(tmp_path / "h.db", readonly=True)
    assert schema_version(conn) == max(v for v, _ in MIGRATIONS) >= 2
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("incidents", "incident_artifacts", "genes", "gene_vectors", "enrichment", "pairs",
              "campaigns", "campaign_members", "assignments", "reviews", "runs"):
        assert t in names


def test_gene_defaults_and_missing_is_unknown():
    g = c.Gene(incident_id="nask-1", family=c.Family.INF, kind="host", extractor="har_hosts@1",
               status=c.GeneStatus.UNAVAILABLE)
    assert g.status == "unavailable" and g.value is None and g.provenance == "OBS"


def test_pair_score_roundtrip_and_validation():
    m = c.GeneMatch(family=c.Family.TXT, kind="phrase", value_a="x", value_b="x", doc_freq=2, bits=5.1,
                    explanation_pl="Ta sama rzadka fraza")
    p = c.PairScore(a="nask-1", b="nask-2", family_bits={"TXT": 5.1}, reasons=[m], total_bits=5.1,
                    probability=0.9, hypothesis=c.Hypothesis.SAME_CAMPAIGN, model="rarity_bits@1")
    assert c.PairScore.model_validate_json(p.model_dump_json()) == p
    with pytest.raises(ValidationError):
        c.PairScore(a="a", b="b", total_bits=0, probability=1.5, hypothesis="independent", model="x")
    with pytest.raises(ValidationError):
        c.Gene(incident_id="i", family="VIS", kind="pdq", extractor="x", bogus=1)


def test_assignment_new_campaign():
    a = c.Assignment(incident_id="new-1", decision=c.AssignmentDecision.NEW_CAMPAIGN,
                     reason_pl="Brak kampanii z wystarczającym wsparciem", model="assign@1")
    assert a.campaign_id is None and a.decision == "new_campaign"
