"""Review semantics (audit A04/A05): approvals publish campaign revisions, rejections and undo behave."""

from __future__ import annotations

from contextlib import closing

from hydra.assign import analyse_input
from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.pipeline import analyse, latest_run, load_clustering
from hydra.review import apply_review, new_review
from tests.core.test_pipeline import PX, corpus, page, test_extractors  # noqa: F401 — fixture


def campaigns_of(settings: Settings, run_id: str | None = None) -> dict[frozenset, object]:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        camps, _ = load_clustering(conn, run_id or latest_run(conn))
    return {frozenset(c.members): c for c in camps}


def review(settings, action, target, **kw):
    w = Writer(settings.db_path)
    try:
        return apply_review(new_review(action, target, **kw), settings, w)
    finally:
        w.close()


def test_revision_is_self_assigned_too(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    """A review publishes a revision run; the open-set stage is recomputed for it (never an empty stage)."""
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    rev = review(settings, "reject_link", {"a": "nask-1", "b": "nask-2"})
    with closing(connect(settings.db_path, readonly=True)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM assignments WHERE run_id=?", (rev,)).fetchone()[0]
        members = conn.execute("SELECT COUNT(DISTINCT incident_id) FROM campaign_members WHERE run_id=?",
                               (rev,)).fetchone()[0]
    assert n == members > 0


def test_approved_new_pattern_is_found_by_the_next_variant(settings: Settings, make_png, tmp_path,
                                                           test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    fresh = '<i data-acct="acct-fresh-77"></i><i data-pixel="5550001112223334"></i> +48 800 900 100'
    page(tmp_path / "A", "fresh-scam.example", fresh)
    a = analyse_input("file", tmp_path / "A", settings, only=test_extractors)
    assert a["assignment"]["decision"] == "new_campaign"
    # without approval the second variant is again a new pattern (no auto-publication)
    page(tmp_path / "B0", "fresh-scam.example", fresh)
    assert analyse_input("file", tmp_path / "B0", settings, only=test_extractors)["assignment"]["decision"] \
        == "new_campaign"
    rid = review(settings, "move_incident", {"incident_id": a["incident_id"], "campaign_id": "new"})
    assert frozenset({a["incident_id"]}) in campaigns_of(settings, rid)
    page(tmp_path / "B", "fresh-scam.example", fresh + " wariant")
    b = analyse_input("file", tmp_path / "B", settings, only=test_extractors)
    assert b["assignment"]["decision"] == "assign"
    assert b["assignment"]["campaign_id"] == campaigns_of(settings)[frozenset({a["incident_id"]})].id


def test_rejected_link_never_changes_the_core(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    before = campaigns_of(settings)
    camp_a = before[frozenset({"nask-1", "nask-2", "nask-3"})]
    page(tmp_path / "C", "evil-a.example", "+48 700 100 100" + PX)
    c = analyse_input("file", tmp_path / "C", settings, only=test_extractors)
    assert c["assignment"]["campaign_id"] == camp_a.id  # candidate only
    rid = review(settings, "reject_link", {"a": c["incident_id"], "b": "nask-1"})
    after = campaigns_of(settings, rid)
    a2 = after[frozenset({"nask-1", "nask-2", "nask-3"})]
    assert [m.model_dump() for m in a2.core_genome] == [m.model_dump() for m in camp_a.core_genome]
    assert not any(c["incident_id"] in k for k in after)


def test_accept_move_merge_split_and_undo(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    original = set(campaigns_of(settings))
    a_id = campaigns_of(settings)[frozenset({"nask-1", "nask-2", "nask-3"})].id
    b_id = campaigns_of(settings)[frozenset({"nask-4", "nask-5"})].id
    w = Writer(settings.db_path)
    try:
        mv = new_review("move_incident", {"incident_id": "nask-3", "campaign_id": "new"})
        r1 = apply_review(mv, settings, w)
        cs = campaigns_of(settings, r1)
        assert frozenset({"nask-3"}) in cs and frozenset({"nask-1", "nask-2"}) in cs
        r2 = apply_review(new_review("undo", {}, undoes=mv.id), settings, w)
        assert set(campaigns_of(settings, r2)) == original  # undo restores the previous membership
        mg = new_review("merge_campaigns", {"campaign_ids": [a_id, b_id]})
        r3 = apply_review(mg, settings, w)
        assert frozenset({"nask-1", "nask-2", "nask-3", "nask-4", "nask-5"}) in campaigns_of(settings, r3)
        merged = campaigns_of(settings, r3)[frozenset({"nask-1", "nask-2", "nask-3", "nask-4", "nask-5"})].id
        r4 = apply_review(new_review("split_campaign", {"campaign_id": merged, "incident_ids": ["nask-4",
                                                                                                "nask-5"]}),
                          settings, w)
        cs = campaigns_of(settings, r4)
        assert frozenset({"nask-4", "nask-5"}) in cs and frozenset({"nask-1", "nask-2", "nask-3"}) in cs
        acc = apply_review(new_review("accept_link", {"a": "nask-6", "b": "nask-1"}), settings, w)
        assert any({"nask-6", "nask-1"} <= set(k) for k in campaigns_of(settings, acc))
    finally:
        w.close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        n_reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        n_events = conn.execute("SELECT COUNT(*) FROM events WHERE type='REVISION_PUBLISHED'").fetchone()[0]
    assert n_reviews == 5 and n_events == 5  # history kept, one revision per review


def test_fresh_analyse_respects_pair_decisions_and_assign_uses_batch_prior(settings: Settings, make_png, tmp_path,
                                                                            test_extractors):  # noqa: F811
    from tests.core.test_corpus_progress_train import ext_dataset, import_ext

    ds = corpus(tmp_path, make_png)
    analyse([ds], settings, only=test_extractors)
    review(settings, "reject_link", {"a": "nask-4", "b": "nask-5"})
    import_ext(settings, ext_dataset(tmp_path, groups=2, per=2))
    res = analyse([ds], settings, only=test_extractors, prior="ext:all")
    assert res.metrics["analyst_constraints"]["cannot_link"] == 1
    assert not any({"nask-4", "nask-5"} <= set(c.members) for c in res.clustering.campaigns)
    page(tmp_path / "N", "evil-a.example", "+48 700 100 100" + PX)
    out = analyse_input("file", tmp_path / "N", settings, only=test_extractors)
    assert out["assignment"]["meta"]["scoring"]["prior"] == "ext:all"


def test_ignore_value_zeroes_one_value_and_undo_restores(settings: Settings, make_png, tmp_path,
                                                         test_extractors):  # noqa: F811
    """ADOPT #2: the analyst moves one value to the boilerplate tier; pairs holding it are rescored; undo restores."""
    import json

    from hydra.review import ignored_values

    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        root = latest_run(conn)

    def pair(run):
        with closing(connect(settings.db_path, readonly=True)) as conn:
            row = conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a='nask-4' AND b='nask-5'",
                               (run,)).fetchone()
        return json.loads(row["detail"])

    w = Writer(settings.db_path)
    try:
        ig = new_review("ignore_value", {"kind": "host", "value": "shop-b.example"})
        r1 = apply_review(ig, settings, w)
        with closing(connect(settings.db_path, readonly=True)) as conn:
            assert ignored_values(conn) == {("host", "shop-b.example")}
        before, after = pair(root), pair(r1)
        assert after["total_bits"] < before["total_bits"]
        assert not any("shop-b.example" in (r.get("value_a") or "") for r in after["reasons"])
        r2 = apply_review(new_review("undo", {}, undoes=ig.id), settings, w)
        assert pair(r2)["total_bits"] == before["total_bits"]
        with closing(connect(settings.db_path, readonly=True)) as conn:
            assert ignored_values(conn) == set()
    finally:
        w.close()

def _views(settings: Settings, run: str, incident: str) -> tuple[str | None, dict, dict]:
    from hydra import dataflow
    from hydra.api_task import _latest_assignments

    with closing(connect(settings.db_path, readonly=True)) as conn:
        latest = {r["incident_id"]: r["campaign_id"] for r in _latest_assignments(conn, run)}
        snap = dataflow.normalise(dataflow.snapshot(conn, run))
        ref = dataflow.normalise(dataflow.recompute(conn, run))
    return latest.get(incident), snap, ref


def test_assigned_material_survives_reviews_and_undo(settings: Settings, make_png, tmp_path,
                                                     test_extractors):  # noqa: F811
    """CR-E2E-5: material assigned against the base run stays attached to every revision (views and live layer),
    and is re-pointed when a review changes the member set (so the id) of its campaign."""
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    camp_a = campaigns_of(settings)[frozenset({"nask-1", "nask-2", "nask-3"})]
    page(tmp_path / "C", "evil-a.example", "+48 700 100 100" + PX)
    c = analyse_input("file", tmp_path / "C", settings, only=test_extractors)
    assert c["assignment"]["campaign_id"] == camp_a.id
    cid = c["incident_id"]
    rev = review(settings, "reject_link", {"a": "nask-4", "b": "nask-5"})  # does not touch campaign A
    got, snap, ref = _views(settings, rev, cid)
    assert got == camp_a.id
    assert cid in snap["incidents"] and snap == ref
    other = next(x for k, x in sorted(campaigns_of(settings, rev).items(), key=lambda t: sorted(t[0]))
                 if "nask-1" not in k)
    # a merge changes the member set (so the id) of campaign A without separating anything
    mv = new_review("merge_campaigns", {"campaign_ids": [camp_a.id, other.id]})
    w = Writer(settings.db_path)
    try:
        rev2 = apply_review(mv, settings, w)
    finally:
        w.close()
    a2 = next(x for k, x in campaigns_of(settings, rev2).items() if {"nask-1", "nask-2", "nask-3"} < k)
    got, snap, ref = _views(settings, rev2, cid)
    assert got == a2.id, "assignment not re-pointed to the revised campaign"
    assert cid in snap["incidents"] and snap == ref
    rev3 = review(settings, "undo", {}, undoes=mv.id)
    got, snap, ref = _views(settings, rev3, cid)
    assert got == camp_a.id
    assert cid in snap["incidents"] and snap == ref
