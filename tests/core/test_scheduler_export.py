"""Scheduler (inbox, ad-library without/with a fake token) and Neo4j export, on the synthetic corpus."""

from __future__ import annotations

import csv
from contextlib import closing

from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.export_neo4j import export_run
from hydra.pipeline import analyse
from hydra.scheduler import job_adlibrary_pull, load_watchlist, run_due, sched_dir, scheduler_status, watch_add
from tests.core.test_pipeline import N_FILLER, N_INC, PX, corpus, page, test_extractors  # noqa: F401 — fixture


def test_neo4j_export(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        counts = export_run(conn, res.run_id, tmp_path / "neo")
    assert counts["incidents"] == N_INC and counts["campaigns"] == 3 + N_FILLER and counts["groups"] == 1
    assert counts["linked_to"] == N_INC * (N_INC - 1) // 2 and counts["genes"] >= 2
    cy = (tmp_path / "neo" / "import.cypher").read_text()
    assert "LINKED_TO" in cy and "MEMBER_OF" in cy and "HAS_GENE" in cy and "IN_GROUP" in cy
    with (tmp_path / "neo" / "genes.csv").open() as fh:
        assert any(r["value"] == "evil-a.example" for r in csv.DictReader(fh))


def test_scheduler_inbox_and_status(settings: Settings, make_png, tmp_path, test_extractors, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HYDRA_META_TOKEN", raising=False)
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    page(sched_dir(settings) / "inbox" / "new1", "evil-a.example", "+48 700 100 100" + PX)
    watch_add(settings, keywords=["evil"], domains=["Evil-A.example"])
    from hydra.genome import base

    real = base.run_all
    monkeypatch.setattr(base, "run_all", lambda inc, ctx, only=None: real(inc, ctx, only=test_extractors))
    import os
    import time

    import hydra.scheduler as sch

    old = time.time() - 60
    for f in (sched_dir(settings) / "inbox").rglob("*"):
        os.utime(f, (old, old))
    (sched_dir(settings) / "inbox" / "fresh.txt").write_text("still copying")  # too new: waits
    (sched_dir(settings) / "inbox" / "broken.zip").write_bytes(b"PK\x03\x04 not a zip")
    os.utime(sched_dir(settings) / "inbox" / "broken.zip", (old, old))
    monkeypatch.setattr(sch, "SETTLE_S", 10.0)
    w = Writer(settings.db_path)
    try:
        st = run_due(settings, w, force=True)
    finally:
        w.close()
    jobs = st["jobs"]
    inbox = jobs["inbox"]["detail"]
    assert inbox["waiting"] == 1
    items = {i["item"]: i for i in inbox["items"]}
    assert items["new1"]["decision"] == "assign"
    assert (sched_dir(settings) / "inbox" / "fresh.txt").exists()
    assert any(p.name.endswith("new1") for p in (sched_dir(settings) / "processed").iterdir())
    adl = jobs["adlibrary_pull"]
    assert adl["status"] == "unavailable" and adl["detail"]["mode"] == "replay"
    assert "HYDRA_META_TOKEN" in adl["detail"]["note"] and "inbox/" in adl["detail"]["note"]
    assert jobs["reenrich_domains"]["status"] == "skipped"
    assert jobs["keyword_watch"]["detail"]["hits"]["evil"]
    assert scheduler_status(settings)["inbox_pending"] == 1  # fresh.txt
    assert load_watchlist(settings)["domains"] == ["evil-a.example"]


def test_adlibrary_with_token_writes_inbox(settings: Settings, monkeypatch):
    from hydra.enrich import adlibrary as al

    monkeypatch.setenv("HYDRA_META_TOKEN", "EAAfake")
    monkeypatch.setitem(al.DELAY, al.SOURCE, 0)
    live = settings.model_copy(update={"enrich": True, "offline": False})
    watch_add(live, keywords=["inwestycja"])

    def fake_get(url, params):
        assert params["search_terms"] == "inwestycja" and params["access_token"] == "EAAfake"
        return 200, {"data": [{"id": "123", "page_name": "P", "ad_creative_bodies": ["Zarabiaj 5000 zł"]}]}

    r = job_adlibrary_pull(live, None, load_watchlist(live), http_get=fake_get)
    assert (r["status"], r["new"], r["mode"]) == ("ok", 1, "live")
    assert (sched_dir(live) / "inbox" / "adlib-123" / "ad_text.txt").read_text() == "Zarabiaj 5000 zł"
    assert job_adlibrary_pull(live, None, load_watchlist(live), http_get=fake_get)["new"] == 0


def test_inbox_failure_goes_to_failed(settings: Settings, monkeypatch):
    import os
    import time

    import hydra.pipeline as pl
    from hydra.scheduler import job_inbox

    item = sched_dir(settings) / "inbox" / "bad.bin"
    item.write_bytes(b"x")
    old = time.time() - 60
    os.utime(item, (old, old))

    def boom(*a, **k):
        raise RuntimeError("cannot parse")

    monkeypatch.setattr(pl, "analyse_input", boom)
    r = job_inbox(settings, None, load_watchlist(settings))
    assert r["failed"] == 1 and any(p.name.endswith("bad.bin") for p in (sched_dir(settings) / "failed").iterdir())
