"""META-COLLECT acceptance: observation -> incident -> open-set decision -> triage -> manipulation -> report, the
durable outbox (crash, lease expiry, repeated requests) and asset acquisition. SYNTHETIC base corpus and ads, test
extractors only (no models)."""

from __future__ import annotations

import base64
import json
import re
from contextlib import closing
from datetime import date

import pytest

from hydra.contracts import Family, Gene
from hydra.db import connect
from hydra.genome import base
from hydra.meta import analysis, collector, media, store
from hydra.meta import report as rep
from tests.core.test_pipeline import corpus, t_page, t_vec
from tests.meta_collection.conftest import TOKEN, ad, configure, count, db_text, rows

URL_RE = re.compile(r"https?://([^/\s]+)")
PHONE_RE = re.compile(r"\+48 ?\d{3} ?\d{3} ?\d{3}")
KNOWN = "https://evil-a.example/?pixel=1234567890123456 +48 700 100 100"
PIXEL_RE = re.compile(r"pixel=(\d{16})")


def t_meta(inc: base.ResolvedIncident, ctx: base.ExtractContext) -> base.ExtractResult:
    """Test-only text extractor: hosts and phones written in the ad text / landing URL list."""
    out = base.ExtractResult()
    for art in inc.by_role("text", "url"):
        text = art.path.read_text(encoding="utf-8", errors="replace")
        ev = {"artifact_id": art.meta.artifact_id}
        for h in URL_RE.findall(text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="host", value=h.lower(),
                                  extractor="t_meta@1", evidence=ev, dependency_group="inf:domain"))
        for px in PIXEL_RE.findall(text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="tracker_id", value=px,
                                  extractor="t_meta@1", evidence=ev, dependency_group="INF:operator:tracker"))
        for p in PHONE_RE.findall(text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.TXT, kind="entity_phone",
                                  value=p.replace(" ", ""), extractor="t_meta@1", evidence=ev))
    return out


@pytest.fixture
def extractors():
    added = []
    for name, fn, fam in (("t_page", t_page, Family.INF), ("t_vec", t_vec, Family.VIS), ("t_meta", t_meta,
                                                                                        Family.INF)):
        if name not in base.REGISTRY:
            base.register(name, family=fam)(fn)
            added.append(name)
    yield ["t_page", "t_vec", "t_meta"]
    for name in added:
        base.REGISTRY.pop(name, None)


@pytest.fixture
def based(online, writer, extractors, make_png, tmp_path):
    """A base analyse run over the synthetic spine corpus (campaign A = evil-a.example + phone 700 100 100)."""
    from hydra.pipeline import analyse

    analyse([corpus(tmp_path, make_png)], online, only=extractors, writer=writer)
    return extractors


def worker(online, writer, clock, extractors, **kw):
    return analysis.MetaWorker(online, writer, only=extractors, clock=clock, **kw)


def collect(online, writer, clock, graph, rows_, term="zysk"):
    configure(writer, term)
    graph.pages[term] = [rows_]
    return collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))


def analyses(online):
    with closing(connect(online.db_path, readonly=True)) as conn:
        return {a["ad_id"]: store.analysis(conn, a["id"]) for a in [
            {"ad_id": r[0], "id": r[1]} for r in conn.execute("SELECT ad_id, id FROM meta_analyses")]}


def test_known_variant_foreign_and_insufficient_data(online, writer, clock, graph, token, based):
    collect(online, writer, clock, graph, [
        ad(1, body="Zarabiaj z nami! https://evil-a.example/start?pixel=1234567890123456 Zadzwoń +48 700 100 100"),
        ad(2, body="Nowa oferta https://other-zone.example/x tel. +48 511 222 333"),
        ad(3, body="", title=""),
    ])
    assert worker(online, writer, clock, based).run_until_idle() == 3
    a = analyses(online)
    known, foreign, empty = a["8000000000001"], a["8000000000002"], a["8000000000003"]
    assert known["decision"] == "assign" and known["hydra_campaign_id"]
    members = rows(online, "SELECT incident_id FROM campaign_members WHERE campaign_id=? AND run_id=?",
                   known["hydra_campaign_id"], known["base_run_id"])
    assert {m[0] for m in members} == {"nask-1", "nask-2", "nask-3"}
    assert known["correlation"]["evidence"] and known["correlation"]["base_run_id"] == known["base_run_id"]
    assert foreign["decision"] == "new_campaign" and foreign["hydra_campaign_id"] is None
    assert empty["decision"] == "abstain"
    # three separate results; triage never confirms; no detector -> never "authentic"
    assert known["triage"]["review_status"] == "unreviewed"
    assert known["triage"]["triage_priority"] == "high"  # known campaign + promise of profit
    assert {r["code"] for r in known["triage"]["reasons"]} >= {"linked_to_known_campaign", "promise_of_profit"}
    assert all(m["status"] in ("not_applicable", "unavailable") for m in known["manipulation"])
    assert known["status"] == "partial" and "bez materiału wizualnego" in known["reason_pl"]
    # the decision is a normal assign run (existing explain views) and the incident is outside the NASK corpus
    assert count(online, "SELECT COUNT(*) FROM assignments WHERE incident_id=?", known["incident_id"]) == 1
    [(src,)] = rows(online, "SELECT source FROM incidents WHERE id=?", known["incident_id"])
    assert src == "ext:meta-adlibrary"
    from hydra.corpus import incidents_in

    with closing(connect(online.db_path, readonly=True)) as conn:
        nask = incidents_in(conn, "nask")
        assert not any(i.startswith("metaad-") for i in nask) and "nask-1" in nask
        assert set(incidents_in(conn, "ext:meta-adlibrary")) == {a_["incident_id"] for a_ in a.values()}
    assert TOKEN not in db_text(online)


def test_same_brand_text_does_not_force_a_campaign(online, writer, clock, graph, token, based):
    collect(online, writer, clock, graph, [ad(1, body="Orlen inwestycja, zysk gwarantowany, bez ryzyka!")])
    worker(online, writer, clock, based).run_until_idle()
    a = analyses(online)["8000000000001"]
    assert a["decision"] != "assign"
    assert a["triage"]["triage_priority"] in ("low", "medium")  # bait words alone never reach "high"
    assert a["review_status"] == "unreviewed"


def test_report_single_and_period_share_one_representation(online, writer, clock, graph, token, based):
    collect(online, writer, clock, graph, [ad(1, body=KNOWN + " zysk")])
    worker(online, writer, clock, based).run_until_idle()
    with closing(connect(online.db_path, readonly=True)) as conn:
        aid = conn.execute("SELECT id FROM meta_analyses").fetchone()[0]
        one = rep.ad_report(conn, aid)
        per = rep.period_report(conn, "live")
    assert one["entries"][0] == per["entries"][0]
    e = one["entries"][0]
    for k in ("source", "timeline", "materials", "correlation", "fraud_indications", "manipulation", "analyst",
              "analysis"):
        assert k in e
    assert one["mode"] == "live" and one["versions"]["extract"] == store.EXTRACT_VERSION
    assert "nie zgłoszenie" in one["semantics_pl"]
    md = rep.to_markdown(one)
    assert "Przesłanki oszustwa" in md and "Sygnały manipulacji" in md and TOKEN not in md + json.dumps(one)


def test_outbox_survives_crash_and_restart_without_duplicates(online, writer, clock, graph, token, based,
                                                              monkeypatch):
    collect(online, writer, clock, graph, [ad(1)])
    w1 = worker(online, writer, clock, based)
    import hydra.pipeline as pl

    real = pl.assign_stored

    def crash(*a, **k):
        raise SystemExit("process killed mid-analysis")

    monkeypatch.setattr(pl, "assign_stored", crash)
    with pytest.raises(SystemExit):
        w1.run_once()
    assert rows(online, "SELECT status FROM meta_outbox") == [("running",)]
    monkeypatch.setattr(pl, "assign_stored", real)
    w2 = worker(online, writer, clock, based)
    assert w2.run_once() is False  # lease still held by the dead owner: no second active owner
    clock.advance(901)
    assert w2.run_once() is True
    assert rows(online, "SELECT status, attempts FROM meta_outbox") == [("partial", 2)]
    # repeated analyst request + another restart: no second analysis, run or report
    job, created = writer.run(lambda c: store.request_analysis(c, "live", "8000000000001", clock()))
    assert created is False and job["status"] == "partial"
    assert worker(online, writer, clock, based).run_until_idle() == 0
    assert count(online, "SELECT COUNT(*) FROM meta_analyses") == 1
    assert count(online, "SELECT COUNT(*) FROM runs WHERE kind='assign'") == 1
    # a failed job is re-queued by the analyst request instead of duplicated
    writer.run(lambda c: c.execute("UPDATE meta_outbox SET status='failed'"))
    job, created = writer.run(lambda c: store.request_analysis(c, "live", "8000000000001", clock()))
    assert created is False and job["status"] == "queued"


def test_new_revision_gets_new_analysis_history_kept(online, writer, clock, graph, token, based):
    collect(online, writer, clock, graph, [ad(1, body="v1 https://evil-a.example/")])
    worker(online, writer, clock, based).run_until_idle()
    graph.pages["zysk"] = [[ad(1, body="v2 https://other-zone.example/")]]
    collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))
    worker(online, writer, clock, based).run_until_idle()
    with closing(connect(online.db_path, readonly=True)) as conn:
        d = store.ad_detail(conn, "live", "8000000000001")
    assert [r["rev"] for r in d["revisions"]] == [2, 1] and len(d["analyses"]) == 2
    assert d["ad"]["current_revision"]["rev"] == 2


def _png(color):
    from tests.conftest import png_bytes

    return png_bytes(color)


def test_synthetic_media_same_text_different_images_and_shared_bytes(online, writer, clock, based):
    red, blue = base64.b64encode(_png((200, 0, 0))).decode(), base64.b64encode(_png((0, 0, 200))).decode()
    rows_ = [dict(ad(1, body="Ten sam tekst"), hydra_synthetic_media=[{"b64": red}]),
             dict(ad(2, body="Ten sam tekst"), hydra_synthetic_media=[{"b64": blue}]),
             dict(ad(3, body="Inny tekst"), hydra_synthetic_media=[{"b64": red}])]
    body = json.dumps({"format": collector.PAGES_FORMAT, "synthetic": True,
                       "pages": [{"query": {"kind": "keyword", "value": "demo"}, "page": 0, "data": {"data": rows_}}]})
    res = collector.replay_body(online, writer, body.encode(), now=clock())
    assert res.mode == "synthetic" and res.new_ads == 3
    assert worker(online, writer, clock, based).run_until_idle() == 3
    arts = rows(online, "SELECT x.artifact_id, r.ad_id FROM meta_artifacts x JOIN meta_revisions r ON "
                        "r.id=x.revision_id WHERE x.status='asset_available'")
    assert len(arts) == 3 and len({a[0] for a in arts}) == 2  # shared bytes deduplicated in CAS, 3 relations kept
    a = analyses(online)
    assert {x["status"] for x in a.values()} == {"done"}
    img = next(m for m in a["8000000000001"]["manipulation"] if m["modality"] == "image")
    assert img["status"] == "unavailable" and "nie oznacza autentyczności" in img["limitations_pl"]
    [(src,)] = rows(online, "SELECT DISTINCT source FROM incidents WHERE id LIKE 'metaad-%'")
    assert src == "ext:meta-adlibrary:synthetic"
    with closing(connect(online.db_path, readonly=True)) as conn:
        r = rep.ad_report(conn, a["8000000000001"]["id"])
    assert r["mode"] == "synthetic" and r["synthetic"] is True and r["mode_label_pl"] == "DANE SYNTETYCZNE"


SNAP = "https://www.facebook.com/ads/archive/render_ad/"


def test_media_fetch_opt_in_blocked_preview_and_bad_files(online, writer, clock, graph, token, based):
    configure(writer, "zysk", media_fetch=True)
    graph.pages["zysk"] = [[ad(1), ad(2), ad(3), ad(4)]]
    collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))
    good = _png((10, 200, 10))
    html_ok = ('<html><img src="https://scontent-waw1-1.xx.fbcdn.net/v/t1/good.png?oh=1">'
               '<img src="https://evil.example/x.png"><video src="https://video.xx.fbcdn.net/v/big.mp4"></video>'
               '<img src="https://internal.fbcdn.net/x.jpg"><svg onload="alert(1)"></svg></html>')
    graph.raw[SNAP + "?id=1"] = (200, {}, html_ok.encode())
    graph.raw[SNAP + "?id=2"] = (200, {}, b"<html><body>Zaloguj sie, aby kontynuowac</body></html>")
    graph.raw[SNAP + "?id=3"] = (200, {}, b'<img src="https://scontent.xx.fbcdn.net/v/fake.png">')
    graph.raw[SNAP + "?id=4"] = (200, {}, b'<img src="https://scontent.xx.fbcdn.net/v/svg.png">')
    graph.raw["https://scontent-waw1-1.xx.fbcdn.net/v/t1/good.png"] = (200, {}, good)
    graph.raw["https://video.xx.fbcdn.net/v/big.mp4"] = (200, {}, b"\x00" * 30_000_001)
    graph.raw["https://scontent.xx.fbcdn.net/v/fake.png"] = (200, {}, b"MZ\x90\x00 not an image")
    graph.raw["https://scontent.xx.fbcdn.net/v/svg.png"] = (200, {}, b'<svg xmlns="http://www.w3.org/2000/svg" '
                                                                     b'onload="alert(1)"/>')
    worker(online, writer, clock, based).run_until_idle()
    with closing(connect(online.db_path, readonly=True)) as conn:
        det = {i: store.ad_detail(conn, "live", f"8000000000{i:03d}") for i in (1, 2, 3, 4)}
    a1 = det[1]["revisions"][0]["artifacts"]
    st = {(x["source"], x["status"]) for x in a1}
    assert ("snapshot_media", "asset_available") in st  # the CDN image
    assert any("spoza hostów" in (x["status_pl"] or "") for x in a1)  # evil.example skipped
    assert any(x["status"] == "invalid_or_unsupported_media" and "bajt" in x["status_pl"] for x in a1)  # too big
    assert any("polityk" in (x["status_pl"] or "") or "odrzucony" in (x["status_pl"] or "") for x in a1)  # 10.x
    assert det[1]["revisions"][0]["collection_status"] == "asset_available"
    assert det[2]["revisions"][0]["collection_status"] == "blocked_or_login_required"
    assert det[3]["revisions"][0]["collection_status"] == "invalid_or_unsupported_media"
    assert det[4]["revisions"][0]["collection_status"] == "invalid_or_unsupported_media"  # SVG never accepted
    # the token went only to the snapshot page, never to a CDN host
    for c in graph.calls:
        if c["_host"] and c["_host"].endswith("fbcdn.net"):
            assert "access_token" not in c
    assert all(c.get("access_token") == TOKEN for c in graph.calls if c["_path"].startswith("/ads/archive"))
    a = analyses(online)
    assert a["8000000000001"]["status"] == "done" and a["8000000000002"]["status"] == "partial"
    assert TOKEN not in db_text(online)


def test_media_fetch_off_by_default_and_attach(online, writer, clock, graph, token, based):
    collect(online, writer, clock, graph, [ad(1)])
    worker(online, writer, clock, based).run_until_idle()
    assert not [c for c in graph.calls if c["_path"].startswith("/ads/archive")]  # no snapshot fetch
    with closing(connect(online.db_path, readonly=True)) as conn:
        arts = store.ad_detail(conn, "live", "8000000000001")["revisions"][0]["artifacts"]
    assert any("media_fetch" in (x["status_pl"] or "") for x in arts)
    with pytest.raises(ValueError):
        media.attach(online, writer, mode="live", ad_id="8000000000001", data=b"<svg onload=1/>", filename="x.svg",
                     now=clock(), max_bytes=1000)
    rid, dg = media.attach(online, writer, mode="live", ad_id="8000000000001", data=_png((1, 2, 3)),
                           filename="kreacja.png", now=clock(), max_bytes=10_000_000)
    writer.run(lambda c: store.request_analysis(c, "live", "8000000000001", clock()))
    worker(online, writer, clock, based).run_until_idle()
    with closing(connect(online.db_path, readonly=True)) as conn:
        d = store.ad_detail(conn, "live", "8000000000001")
    assert d["ad"]["current_revision"]["rev"] == 2 and d["analyses"][0]["status"] == "done"
    assert any(x["source"] == "analyst_upload" and x["sha256"] == dg for x in d["revisions"][0]["artifacts"])


def test_review_history_and_alert_counters(online, writer, clock, graph, token, based):
    collect(online, writer, clock, graph, [ad(1, body=KNOWN + " zysk")])
    worker(online, writer, clock, based).run_until_idle()
    aid = rows(online, "SELECT id FROM meta_analyses")[0][0]
    with closing(connect(online.db_path, readonly=True)) as conn:
        assert store.counters(conn, "live")["alerts_open"] == 1
    writer.run(lambda c: store.review(c, aid, "confirmed_by_analyst", "analityk1", "zgodne", clock()))
    writer.run(lambda c: store.review(c, aid, "dismissed_by_analyst", "analityk2", None, clock()))
    with closing(connect(online.db_path, readonly=True)) as conn:
        a = store.analysis(conn, aid)
        c = store.counters(conn, "live")
    assert [h["decision"] for h in a["review"]["history"]] == ["confirmed_by_analyst", "dismissed_by_analyst"]
    assert c["alerts_open"] == 0 and c["analyst_dismissed"] == 1


def test_field_pattern_triage_beyond_keywords(online, writer, clock, graph, token, based):
    body = "Znany ekonomista ujawnił sekretny sposób: akcje Orlen, zarabiaj 900 zł dziennie bez ryzyka!"
    collect(online, writer, clock, graph, [ad(1, body=body, page="501", page_name="Orlen Inwestycje PL"),
                                           ad(2, body=body, page="502"), ad(3, body=body, page="503"),
                                           ad(4, body="Promocja butów w sklepie", page="504")])
    worker(online, writer, clock, based).run_until_idle()
    a = analyses(online)
    codes = {r["code"] for r in a["8000000000001"]["triage"]["reasons"]}
    assert codes >= {"brand_in_page_name", "state_company_investment", "endorsement_story",
                     "specific_income_promise", "same_text_many_pages", "promise_of_profit"}
    assert a["8000000000001"]["triage"]["triage_priority"] == "high"
    assert a["8000000000001"]["review_status"] == "unreviewed"  # never a confirmation
    assert a["8000000000004"]["triage"]["triage_priority"] == "unknown"
    assert all(r["rule_version"] == "meta-triage@2" for r in a["8000000000001"]["triage"]["reasons"])


ROBOTS = "https://www.facebook.com/robots.txt"


def test_snapshots_respect_robots_txt(online, writer, clock, graph, token, based, monkeypatch):
    monkeypatch.setenv("HYDRA_META_SNAPSHOTS", "1")  # operator flag, same effect as the admin toggle
    collect(online, writer, clock, graph, [ad(1), ad(2)])
    graph.raw[ROBOTS] = (200, {}, b"User-agent: Googlebot\nAllow: /ads/\n\nUser-agent: *\nDisallow: /\n")
    worker(online, writer, clock, based).run_until_idle()
    snaps = [c for c in graph.calls if c["_path"].startswith("/ads/archive")]
    robots = [c for c in graph.calls if c["_path"] == "/robots.txt"]
    assert snaps == [] and len(robots) == 1  # robots.txt read once (cached), no snapshot request
    with closing(connect(online.db_path, readonly=True)) as conn:
        arts = store.ad_detail(conn, "live", "8000000000002")["revisions"][0]["artifacts"]
    blocked = [x for x in arts if x["role"] == "ad_media"][0]
    assert blocked["status"] == "blocked_or_login_required" and "robots.txt" in blocked["status_pl"]


def test_snapshot_html_scrubbed_landing_links_become_genes(online, writer, clock, graph, token, based,
                                                          monkeypatch):
    monkeypatch.setenv("HYDRA_META_SNAPSHOTS", "1")
    collect(online, writer, clock, graph, [ad(1, body="Zarabiaj bez ryzyka")])
    graph.raw[ROBOTS] = (200, {}, b"User-agent: *\nAllow: /\n")
    html = (f'<html><script>var t="{TOKEN}";</script><a href="https://l.facebook.com/l.php?u=https%3A%2F%2F'
            f'evil-a.example%2Foffer%3Fpixel%3D1234567890123456&h=x">Sprawdź</a>'
            f'<a href="https://www.facebook.com/x?access_token={TOKEN}">fb</a>'
            '<img src="https://scontent.xx.fbcdn.net/v/ok.png"></html>')
    graph.raw[SNAP + "?id=1"] = (200, {}, html.encode())
    graph.raw["https://scontent.xx.fbcdn.net/v/ok.png"] = (200, {}, _png((5, 5, 5)))
    worker(online, writer, clock, based).run_until_idle()
    with closing(connect(online.db_path, readonly=True)) as conn:
        arts = store.ad_detail(conn, "live", "8000000000001")["revisions"][0]["artifacts"]
        a = store.analysis(conn, conn.execute("SELECT id FROM meta_analyses").fetchone()[0])
        genes = {(r[0], r[1]) for r in conn.execute("SELECT kind, value FROM genes WHERE incident_id=?",
                                                     (a["incident_id"],))}
    snap = next(x for x in arts if x["role"] == "snapshot_html")
    assert snap["derived"] is True and snap["sha256"]
    from hydra.storage import ContentStore

    stored = ContentStore(online.cas_dir).read_bytes(snap["sha256"]).decode()
    assert TOKEN not in stored and "access_token=[REDACTED]" in stored
    assert ("host", "evil-a.example") in genes  # landing link from the snapshot -> INF gene
    assert ("tracker_id", "1234567890123456") in genes
    assert a["status"] == "done" and a["decision"] == "assign"  # the snapshot link carried the operator evidence
    assert TOKEN not in db_text(online)


def test_snapshot_throttle_spacing(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(media, "SNAPSHOT_MIN_INTERVAL_S", 2.0)
    media._THROTTLE["last"] = -1e9
    media.throttle(sleep=slept.append)
    media.throttle(sleep=slept.append)
    assert slept and 1.5 < slept[0] <= 2.0
