"""Astra audit reproducers for the Meta collector: CR-ASTRA-SW-07, SW-08, SW-09 (audit of f450a70, 2026-11-22).

Synthetic inputs only. The three original assertions are kept unchanged from the audit evidence
(astra-software-evidence-a370d4d/test_meta_confirmed.py); extra checks below them only add conditions.
"""
from __future__ import annotations

import json
from contextlib import closing

from hydra.db import connect
from hydra.meta import report, store
from tests.meta_collection.conftest import ad, configure, rows
from tests.meta_collection.test_analysis import extractors, worker  # noqa: F401
from tests.meta_collection.test_collect import cycle


def test_sw07_return_to_previous_creative_sets_current_revision(online, writer, clock, graph, token):
    configure(writer, "astra-synthetic")
    versions = {}
    for body in ("Synthetic creative A", "Synthetic creative B", "Synthetic creative A"):
        graph.pages["astra-synthetic"] = [[ad(1, body=body)]]
        result = cycle(online, writer, clock)
        assert result.records == 1 and result.status == "ok"
        [(current,)] = rows(online, "SELECT r.content FROM meta_ads a JOIN meta_revisions r ON r.id=a.current_rev_id")
        versions[clock()] = json.loads(current)["creative"]["creative_bodies"]
        clock.advance(60)
    [(last, current)] = rows(
        online, "SELECT o.revision_id, a.current_rev_id FROM meta_observations o "
        "JOIN meta_ads a ON o.mode=a.mode AND o.ad_id=a.ad_id ORDER BY o.id DESC LIMIT 1",
    )
    print({"shown_bodies_by_time": versions, "last_observation_revision": last, "current_revision": current})
    assert last == current


def test_sw08_queued_analysis_context_matches_its_idempotency_key(
    online, writer, clock, graph, token, extractors,  # noqa: F811 -- imported pytest fixture
):
    configure(writer, "astra-synthetic")
    graph.pages["astra-synthetic"] = [[ad(1)]]
    cycle(online, writer, clock)  # No base run when the durable job is queued.
    [(queued_key, sha)] = rows(
        online, "SELECT o.idem_key, r.content_sha256 FROM meta_outbox o "
        "JOIN meta_revisions r ON r.id=o.revision_id",
    )
    assert queued_key == store.idem_key("live", ad(1)["id"], sha, "none")
    # A new, empty synthetic base appears before the worker starts. It is a real run row, no scorer patch.
    writer.run(lambda c: c.execute(
        "INSERT INTO runs(id,kind,created_at,finished_at) VALUES "
        "('astra-new-base','analyse','2026-11-22T04:00:00Z','2026-11-22T04:00:00Z')",
    ))
    assert worker(online, writer, clock, extractors).run_until_idle() == 1
    [(key, actual_context)] = rows(online, "SELECT idem_key, context_version FROM meta_analyses")
    repeated, created = writer.run(lambda c: store.request_analysis(c, "live", ad(1)["id"], clock()))
    print({"queued_key": queued_key, "stored_key": key, "actual_context": actual_context,
           "key_for_actual_context": store.idem_key("live", ad(1)["id"], sha, actual_context),
           "duplicate_request_created": created, "repeated_request_key": repeated["idem_key"]})
    assert key == store.idem_key("live", ad(1)["id"], sha, actual_context)
    assert created is False


def test_sw09_review_does_not_rewrite_analysis_time(online, writer, clock, graph, token, extractors):  # noqa: F811
    configure(writer, "astra-synthetic")
    graph.pages["astra-synthetic"] = [[ad(1)]]
    cycle(online, writer, clock)
    assert worker(online, writer, clock, extractors).run_until_idle() == 1
    [(aid,)] = rows(online, "SELECT id FROM meta_analyses")
    with closing(connect(online.db_path, readonly=True)) as conn:
        before = report.ad_entry(conn, aid)
    assert before is not None
    clock.advance(86400)
    assert writer.run(lambda c: store.review(c, aid, "dismissed_by_analyst", "Synthetic Analyst",
                                            "Synthetic review note", clock()))
    with closing(connect(online.db_path, readonly=True)) as conn:
        after = report.ad_entry(conn, aid)
    assert after is not None
    print({"before": before["timeline"]["analysed_at"], "after": after["timeline"]["analysed_at"],
           "review": after["analyst"]})
    assert after["timeline"]["analysed_at"] == before["timeline"]["analysed_at"]


def test_sw07_history_kept_without_third_copy_and_repeat_is_stable(online, writer, clock, graph, token):
    configure(writer, "astra-synthetic")
    for body in ("Synthetic creative A", "Synthetic creative B", "Synthetic creative A", "Synthetic creative A"):
        graph.pages["astra-synthetic"] = [[ad(1, body=body)]]
        cycle(online, writer, clock)
        clock.advance(60)
    revs = rows(online, "SELECT rev, content FROM meta_revisions ORDER BY rev")
    assert [r[0] for r in revs] == [1, 2]  # A and B only, history kept
    assert len(rows(online, "SELECT id FROM meta_observations")) == 4
    [(cur_rev,)] = rows(online, "SELECT r.rev FROM meta_ads a JOIN meta_revisions r ON r.id=a.current_rev_id")
    assert cur_rev == 1


def test_sw08_rekeyed_job_supersedes_equivalent_job(
    online, writer, clock, graph, token, extractors,  # noqa: F811 -- imported pytest fixture
):
    configure(writer, "astra-synthetic")
    graph.pages["astra-synthetic"] = [[ad(1)]]
    cycle(online, writer, clock)  # queued with context "none"
    writer.run(lambda c: c.execute(
        "INSERT INTO runs(id,kind,created_at,finished_at) VALUES "
        "('astra-new-base','analyse','2026-11-22T04:00:00Z','2026-11-22T04:00:00Z')",
    ))
    # An equivalent job for the new context is requested before the worker runs: one analysis, not two.
    _, created = writer.run(lambda c: store.request_analysis(c, "live", ad(1)["id"], clock()))
    assert created is True
    assert worker(online, writer, clock, extractors).run_until_idle() == 1
    assert len(rows(online, "SELECT id FROM meta_analyses")) == 1
    statuses = sorted(r[0] for r in rows(online, "SELECT status FROM meta_outbox"))
    assert "superseded" in statuses and len(statuses) == 2


def test_sw09_review_time_is_in_history(online, writer, clock, graph, token, extractors):  # noqa: F811
    configure(writer, "astra-synthetic")
    graph.pages["astra-synthetic"] = [[ad(1)]]
    cycle(online, writer, clock)
    assert worker(online, writer, clock, extractors).run_until_idle() == 1
    [(aid,)] = rows(online, "SELECT id FROM meta_analyses")
    clock.advance(86400)
    assert writer.run(lambda c: store.review(c, aid, "confirmed_by_analyst", "Synthetic Analyst", None, clock()))
    with closing(connect(online.db_path, readonly=True)) as conn:
        entry = report.ad_entry(conn, aid)
    assert entry is not None
    history = entry["analyst"]["history"]
    assert history and entry["timeline"]["reviewed_at"] == history[-1]["created_at"]
    assert entry["timeline"]["reviewed_at"] > entry["timeline"]["analysed_at"]
