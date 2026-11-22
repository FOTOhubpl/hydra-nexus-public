"""CR-ASTRA-RES-4 / U06: a replay sees a gene only from the moment HYDRA actually had it (SYNTHETIC store).

Scenario: an incident is imported at t0 and extracted right after; at t1 a re-extraction / enrichment adds an
operator identifier. A replay view for t0 < t < t1 must not contain that identifier, even though the incident
itself (its container) existed since t0.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from hydra import events
from hydra.api_hunt import store_observations
from hydra.contracts import Gene
from hydra.db import connect, migrate
from hydra.genome.base import ExtractResult
from hydra.hunt.hypotheses import Ledger, issue
from hydra.hunt.records import DatasetKind, Mode, Observation, Tier, Trait, sha256_of
from hydra.hunt.replay import KnowledgeView, TemporalLeak
from hydra.hunt.signatures import build_signature
from hydra.hunt.verify import check, resolve
from hydra.pipeline import _persist_genes

T0 = "2026-11-20T10:00:00.000+00:00"      # import
T0X = "2026-11-20T10:01:00.000+00:00"     # first extraction completed
TMID = "2026-11-20T12:00:00.000+00:00"    # replay decision time
T1 = "2026-11-20T14:00:00.000+00:00"      # enrichment / re-extraction completed
T2 = "2026-11-20T16:00:00.000+00:00"
LATE = "2026-11-20T18:00:00.000+00:00"


def _gene(iid: str, kind: str, value: str, family: str = "INF", extractor: str = "har_hosts@1",
          enrichment_key: str | None = None) -> Gene:
    ev = {"enrichment_key": enrichment_key} if enrichment_key else {}
    return Gene(incident_id=iid, family=family, kind=kind, value=value, extractor=extractor, evidence=ev)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "hydra.db"
    with closing(connect(p)) as conn:
        migrate(conn)
    return p


def _import(db: Path, iid: str, created_at: str, observed_at: str | None = None) -> None:
    with closing(connect(db)) as conn:
        conn.execute("INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta, created_at)"
                     " VALUES (?, 'test', ?, ?, ?, NULL, '{}', ?)", (iid, iid, iid, observed_at, created_at))
        conn.commit()


def _extract(db: Path, iid: str, genes: list[Gene], at: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "utcnow", lambda: at)
    with closing(connect(db)) as conn:
        meta = json.loads(conn.execute("SELECT meta FROM incidents WHERE id = ?", (iid,)).fetchone()["meta"])
        _persist_genes(conn, iid, ExtractResult(genes=genes), f"fp-{at}", meta)
        conn.commit()
    monkeypatch.undo()


def _keys(view: KnowledgeView, ref: str) -> set[str]:
    return {t.key for o in view.get(ref) for t in o.traits}


def test_enrichment_after_import_is_not_visible_before_it_completed(db: Path, monkeypatch: pytest.MonkeyPatch):
    _import(db, "inc-1", T0)
    _extract(db, "inc-1", [_gene("inc-1", "domain", "shop-a.example")], T0X, monkeypatch)
    _extract(db, "inc-1", [_gene("inc-1", "domain", "shop-a.example"),
                           _gene("inc-1", "tracker_id", "PX-777", extractor="enrich_trackers@1",
                                 enrichment_key="k-px")], T1, monkeypatch)
    obs = store_observations(db)
    mid = KnowledgeView(obs, TMID)
    assert "domain=shop-a.example" in _keys(mid, "inc-1")
    assert "tracker_id=PX-777" not in _keys(mid, "inc-1"), "later enrichment back-dated to import time"
    # the unchanged gene keeps its first availability; the new one becomes visible only at t1
    assert "tracker_id=PX-777" in _keys(KnowledgeView(obs, T1), "inc-1")
    # before the first extraction completed nothing of the incident is known (import alone carries no genes)
    with pytest.raises(TemporalLeak):
        KnowledgeView(obs, T0).get("inc-1")
    latest = [o for o in obs if o.ref == "inc-1"][-1]
    assert latest.collected_at == T0 and latest.completed_at == T1 and latest.available_at == T1
    first = next(o for o in obs if o.ref == "inc-1")
    assert latest.evidence_version and latest.evidence_version != first.evidence_version


def test_late_document_with_early_event_date_does_not_move_knowledge_back(db: Path, monkeypatch: pytest.MonkeyPatch):
    _import(db, "late", T2, observed_at="2026-11-01T00:00:00Z")  # delivered at t2, event dated weeks earlier
    _extract(db, "late", [_gene("late", "tracker_id", "PX-1")], T2, monkeypatch)
    obs = store_observations(db)
    with pytest.raises(TemporalLeak):
        KnowledgeView(obs, TMID).get("late")
    o = KnowledgeView(obs, LATE).get("late")[0]
    assert o.event_time == "2026-11-01T00:00:00Z" and o.available_at == T2


def test_correction_keeps_previous_version_reproducible(db: Path, monkeypatch: pytest.MonkeyPatch):
    _import(db, "inc-c", T0)
    _extract(db, "inc-c", [_gene("inc-c", "tracker_id", "PX-OLD")], T0X, monkeypatch)
    _extract(db, "inc-c", [_gene("inc-c", "tracker_id", "PX-NEW")], T1, monkeypatch)
    obs = store_observations(db)
    before, after = KnowledgeView(obs, TMID).get("inc-c"), KnowledgeView(obs, T2).get("inc-c")
    assert len(before) == len(after) == 1  # one current version per incident in a view
    assert _keys(KnowledgeView(obs, TMID), "inc-c") == {"tracker_id=PX-OLD"}
    assert _keys(KnowledgeView(obs, T2), "inc-c") == {"tracker_id=PX-NEW"}
    # rebuilding the store observations gives the same version ids and payload hashes (reproducible)
    again = store_observations(db)
    assert [(o.observation_id, o.evidence_version, o.payload_sha256) for o in obs] == \
           [(o.observation_id, o.evidence_version, o.payload_sha256) for o in again]
    with closing(connect(db, readonly=True)) as conn:
        meta = json.loads(conn.execute("SELECT meta FROM incidents WHERE id = 'inc-c'").fetchone()["meta"])
    vers = meta["evidence_versions"]
    assert [v["completed_at"] for v in vers] == [T0X, T1] and vers[0]["id"] != vers[1]["id"]
    assert meta["evidence_retired"][0]["value"] == "PX-OLD" and meta["evidence_retired"][0]["retired_at"] == T1


def test_unknown_gene_time_is_never_backdated(db: Path, monkeypatch: pytest.MonkeyPatch):
    _import(db, "inc-u", T0)
    _extract(db, "inc-u", [_gene("inc-u", "domain", "u.example")], T0X, monkeypatch)
    with closing(connect(db)) as conn:  # a legacy / foreign row whose availability nobody recorded
        conn.execute("INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence,"
                     " extractor, created_at) VALUES ('inc-u', 'INF', 'tracker_id', 'PX-U', 1, 'observed', 'OBS',"
                     " '{}', 'legacy@0', '')")
        conn.commit()
    obs = store_observations(db)
    for cut in (TMID, LATE):
        assert "tracker_id=PX-U" not in _keys(KnowledgeView(obs, cut), "inc-u")
    assert all(o.meta.get("unknown_time_traits") == 1 for o in obs if o.ref == "inc-u")


def _t(kind: str, value: str, tier: Tier, source: str = "har") -> Trait:
    g = {Tier.OPERATOR: "operator", Tier.KIT: "page_structure", Tier.DECORATION: "decoration"}[tier]
    return Trait(kind=kind, value=value, tier=tier, group=g, dep=f"{source}:{kind}", source=source)


def test_lead_time_counts_from_evidence_availability(db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A candidate imported before the hypothesis but enriched after it confirms only at the enrichment time."""
    members = {"m1": [_t("tracker_id", "PX-9", Tier.OPERATOR)], "m2": [_t("tracker_id", "PX-9", Tier.OPERATOR)]}
    sig = build_signature("C", "C@r", members, {}, approval_basis="replay_seed", dataset_kind=DatasetKind.SYNTHETIC)
    seed = [Observation(observation_id=f"seed:{m}", source="local_store", ref=m, available_at=T0,
                        available_at_basis="SYNTHETIC", payload_sha256=sha256_of(m), evidence_pointer=m,
                        traits=ts, dataset_kind=DatasetKind.SYNTHETIC) for m, ts in members.items()]
    _import(db, "cand", T0)
    _extract(db, "cand", [_gene("cand", "domain", "cand.example")], T0X, monkeypatch)
    _extract(db, "cand", [_gene("cand", "domain", "cand.example"),
                          _gene("cand", "tracker_id", "PX-9", extractor="enrich@1", enrichment_key="k")],
             T1, monkeypatch)
    obs = seed + store_observations(db)
    view = KnowledgeView(obs, TMID)
    h = issue(sig, view, issued_at=TMID, horizon_hours=24, sources=["local_store"], query_budget=1,
              ledger=Ledger(tmp_path / "l.jsonl"), mode=Mode.TEMPORAL_REPLAY)
    assert check(h, view.get("cand")[0])["verdict"] == "unrelated"  # at decision time the pixel was not known
    later = [o for o in obs if o.available_at > TMID]
    res = resolve(h, [check(h, o) for o in later], LATE, coverage_complete=True)
    assert res["status"] == "confirmed" and res["first_confirming"]["available_at"] == T1
