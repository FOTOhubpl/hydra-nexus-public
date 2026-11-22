from __future__ import annotations

import json

import pytest
from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.enrich import adlibrary as al
from hydra.enrich.client import Enricher

TOKEN = "EAAtestTOKEN0123456789abcdefABCDEF"


def _row(i, body, url, start="2026-10-05T10:00:00+0000"):
    return {
        "id": str(1000000000000 + i),
        "page_id": "555",
        "page_name": "Fikcyjne Wiadomości",
        "ad_creative_bodies": [body],
        "ad_creative_link_urls": [url],
        "ad_creative_link_captions": ["x.example"],
        "ad_creative_link_titles": ["Tytuł"],
        "ad_delivery_start_time": start,
        "publisher_platforms": ["facebook"],
        "ad_snapshot_url": f"https://www.facebook.com/ads/archive/render_ad/?id={1000000000000 + i}"
        f"&access_token={TOKEN}",
    }


class FakeGraph:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, params):
        self.calls.append(params)
        assert params["access_token"] == TOKEN
        return self.pages[len(self.calls) - 1]


def _rules(**kw):
    base = dict(search_terms=["inwestycja"], keywords=["zarobek"], max_domain_age_days=None, max_pages=3)
    base.update(kw)
    return al.Rules(**base)


def test_no_token_is_unavailable_with_polish_message(tmp_path, monkeypatch):
    monkeypatch.delenv("HYDRA_META_TOKEN", raising=False)
    res = al.search(_rules(), enricher=Enricher(cache_dir=tmp_path, live=True))
    assert res.status == "unavailable" and "HYDRA_META_TOKEN" in res.message_pl and "tożsamość" in res.message_pl
    assert res.ads == []


def test_paged_search_cached_token_never_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_META_TOKEN", TOKEN)
    monkeypatch.setitem(al.DELAY, al.SOURCE, 0)
    pages = [
        (
            200,
            {
                "data": [_row(1, "Szybki zarobek na platformie", "https://lure-fixture.live/a?sub1=1")],
                "paging": {"cursors": {"after": "C1"}, "next": f"https://graph.facebook.com/x?access_token={TOKEN}"},
            },
        ),
        (429, {"error": {"code": 4, "message": "rate limit"}}),
        (200, {"data": [_row(2, "Przepis na ciasto", "https://bakery-fixture.pl/")], "paging": {}}),
    ]
    g = FakeGraph(pages)
    sleeps = []
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch
    enr = Enricher(cache_dir=tmp_path, live=True)
    res = al.search(_rules(), enricher=enr, http_get=g, sleep=sleeps.append)
    assert res.status == "ok"
    assert [a.ad_id for a in res.ads] == ["1000000000001"]  # only the keyword hit is flagged
    assert res.ads[0].reasons == ["keyword:zarobek"]
    assert g.calls[1]["after"] == "C1" and sleeps  # paged + backed off on 429
    assert TOKEN not in res.ads[0].snapshot_url
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert TOKEN not in f.read_text()
    assert all(TOKEN not in json.dumps(r.model_dump()) for r in res.records)
    # replay: no live, no token -> same result from cache, no HTTP
    monkeypatch.delenv("HYDRA_META_TOKEN")
    res2 = al.search(
        _rules(),
        enricher=Enricher(cache_dir=tmp_path, live=False),
        http_get=lambda *a: pytest.fail("network in replay"),
    )
    assert [a.ad_id for a in res2.ads] == ["1000000000001"]


def test_rule_engine_reasons(tmp_path):
    rdap_dir = tmp_path / "rdap"
    rdap_dir.mkdir()
    (rdap_dir / "lure-fixture.live.json").write_text(
        json.dumps(
            {
                "status": 200,
                "fetched_at": "2026-10-06T00:00:00+00:00",
                "data": {"events": [{"eventAction": "registration", "eventDate": "2026-10-01T00:00:00Z"}]},
            }
        )
    )
    ad = al.AdRecord(
        ad_id="1",
        source="meta_api",
        bodies=["Rafał Testowy poleca Orzeł Fikcjoris, zarobek!"],
        link_urls=["https://www.lure-fixture.live/x"],
        delivery_start="2026-10-05",
    )
    rules = al.Rules(
        keywords=["zarobek"],
        celebrities=["Rafal Testowy"],
        platform_names=["Orzeł Fikcjoris"],
        known_skeletons={"L3.L4-L7.live"},
        date_min="2026-10-01",
        date_max="2026-10-31",
        max_domain_age_days=30,
    )
    r = al.evaluate(ad, rules, enricher=Enricher(cache_dir=tmp_path, live=False))
    assert {
        "keyword:zarobek",
        "celebrity:Rafal Testowy",
        "platform_name:Orzeł Fikcjoris",
        "date_window:2026-10-05",
        "url_skeleton:L3.L4-L7.live",
        "new_domain:lure-fixture.live:4d",
    } <= set(r)
    rules2 = al.Rules(keywords=[], known_hosts={"lure-fixture.live"}, max_domain_age_days=None)
    assert al.evaluate(ad, rules2) == ["known_host:www.lure-fixture.live"]


def test_manual_imports(tmp_path):
    csvf = tmp_path / "export.csv"
    csvf.write_text(
        "Library ID;Page name;Ad creative body;Link URL\n"
        "2000000000001;Strona;Gwarantowany zysk bez ryzyka;https://lure2-fixture.top/\n",
        encoding="utf-8",
    )
    jf = tmp_path / "api.json"
    jf.write_text(json.dumps({"data": [_row(3, "zarobek", "https://x-fixture.live/")]}))
    info = tmp_path / "Czytać [3000000000001_3].info.json"
    info.write_text(
        json.dumps(
            {
                "id": "3000000000001_3",
                "playlist_id": "3000000000001",
                "title": "Czytać",
                "playlist_uploader": "FikcyjnaStrona",
                "playlist_uploader_id": "777",
                "webpage_url": "https://www.facebook.com/ads/library/?id=3000000000001",
                "extractor": "facebook:ads",
            }
        )
    )
    html = tmp_path / "snapshot.html"
    html.write_text(
        "<html><body><div>Identyfikator biblioteki: 4000000000001</div><p>Szybki zarobek</p>"
        "<a href='https://l.facebook.com/l.php?u=https%3A%2F%2Flure3-fixture.live%2Fp'>x</a></body></html>"
    )
    img = tmp_path / "ad_5000000000001.png"
    img.write_bytes(b"\x89PNG fake")
    got = {a.source: a for p in (csvf, jf, info, html, img) for a in al.import_file(p)}
    assert got["manual_csv"].ad_id == "2000000000001" and got["manual_csv"].link_urls == ["https://lure2-fixture.top/"]
    assert TOKEN not in got["manual_json"].snapshot_url
    assert got["ytdlp_info"].ad_id == "3000000000001" and got["ytdlp_info"].page_id == "777"
    assert got["snapshot_html"].ad_id == "4000000000001"
    assert got["snapshot_html"].link_urls == ["https://lure3-fixture.live/p"]
    assert got["screenshot"].ad_id == "5000000000001"
    assert al.import_file(tmp_path / "nothing.bin") == []
    res = al.search(
        al.Rules(keywords=["zysk"], max_domain_age_days=None, import_paths=[csvf, img]),
        enricher=Enricher(cache_dir=tmp_path / "c", live=False),
    )
    assert res.status == "ok" and [a.ad_id for a in res.ads] == ["2000000000001"]


def test_to_incident_feeds_inf_extractors(tmp_path, settings):
    from hydra.genome import base
    from hydra.genome.har import inf_url

    ad = al._from_api(_row(9, "zarobek", "https://lure-fixture.live/a?sub1=camp7"), None, None)
    ad.reasons = ["keyword:zarobek"]
    inc = al.to_incident(ad, tmp_path / "inc")
    assert inc.incident.id == "adlib-1000000000009"
    roles = {a.meta.role for a in inc.artifacts}
    assert {"platform_metadata", "text", "url"} <= roles
    assert all(TOKEN not in a.path.read_text() for a in inc.artifacts)
    kv = {(g.kind, g.value) for g in inf_url(inc, base.ExtractContext(settings=settings, work_dir=tmp_path)).genes}
    assert ("landing_host", "lure-fixture.live") in kv and ("tds_value", "sub1=camp7") in kv
