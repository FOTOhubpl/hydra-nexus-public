"""GAP-AUDIT B16: Ad Library in the hint search and the scheduler (token sources, rule filter, inbox exports).
Synthetic ads, fictional ids and domains, mocked HTTP; no network."""

from __future__ import annotations

import json
import os
import time
from datetime import date

import pytest

from hydra.enrich import adlibrary as al
from hydra.enrich.client import Enricher
from hydra.scheduler import job_adlibrary_pull, job_inbox, load_watchlist, sched_dir, watch_add

TOKEN = "EAAhintTOKEN1234567890qwertyQWERTY"


def _row(n: int, body: str) -> dict:
    return {"id": f"8100000000{n:03d}", "page_id": "9100000001", "page_name": "Fikcyjny Portal",
            "ad_creative_bodies": [body], "ad_creative_link_urls": [f"https://lure-{n}.example/"],
            "ad_delivery_start_time": "2026-11-10",
            "ad_snapshot_url": f"https://www.facebook.com/ads/archive/render_ad/?id={n}&access_token={TOKEN}"}


class Graph:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def __call__(self, url, params):
        assert params["access_token"] == TOKEN
        self.calls += 1
        return 200, {"data": [] if "search_page_ids" in params else self.rows}


def _no_token_anywhere(tmp_path) -> str:
    return "".join(p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".env")


def test_token_sources(monkeypatch, tmp_path):
    assert al._token() is None and not al.token_configured()  # conftest isolates the developer's real token
    f = tmp_path / "meta.env"
    f.write_text(f"# Meta\nexport META_AD_LIBRARY_TOKEN='{TOKEN}'\n")
    monkeypatch.setenv("HYDRA_META_ENV_FILE", str(f))
    assert al._token() == TOKEN
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", "EAAfromENV")
    assert al._token() == "EAAfromENV"  # environment wins over the file


def test_hint_not_configured_offline_live_and_replay(monkeypatch, settings, tmp_path):
    cache = tmp_path / "enrich"
    r = al.hint_lookup("szybki zysk", settings, enricher=Enricher(cache_dir=cache, live=True))
    assert r["status"] == "not_configured" and "META_AD_LIBRARY_TOKEN" in r["note"] and r["ads"] == []
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", TOKEN)
    r = al.hint_lookup("szybki zysk", settings)  # default Settings: HYDRA_OFFLINE=1 -> no call, empty cache
    assert r["status"] == "offline" and r["token_configured"]
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)
    g = Graph([_row(1, "Szybki zysk z platformy, zarobek bez ryzyka"), _row(2, "Przepis na sernik")])
    live = al.hint_lookup("szybki zysk", settings, enricher=Enricher(cache_dir=cache, live=True), http_get=g,
                          sleep=lambda s: None)
    assert live["status"] == "ok" and live["mode"] == "live" and live["n_ads"] == 2 and g.calls == 1
    top = live["ads"][0]
    assert top["ad_id"] == "8100000000001" and "keyword:szybki zysk" in top["reasons"]
    assert live["ads"][1]["reasons"] == []
    assert TOKEN not in json.dumps(live) and TOKEN not in _no_token_anywhere(tmp_path)
    monkeypatch.delenv("META_AD_LIBRARY_TOKEN")
    replay = al.hint_lookup("szybki zysk", settings, enricher=Enricher(cache_dir=cache, live=False))
    assert replay["status"] == "ok" and replay["mode"] == "replay"
    assert [a["ad_id"] for a in replay["ads"]] == [a["ad_id"] for a in live["ads"]]


def test_scheduler_stores_only_flagged_ads_with_reasons(monkeypatch, settings, tmp_path):
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", TOKEN)
    monkeypatch.setitem(al.DELAY, al.SOURCE, 0)
    monkeypatch.setenv("HYDRA_ENRICH", "1")
    monkeypatch.setenv("HYDRA_OFFLINE", "0")
    s = settings.model_copy(update={"enrich": True, "offline": False})
    watch_add(s, keywords=["platforma"])
    wl = load_watchlist(s)
    wl["max_domain_age_days"] = None  # no RDAP in this test
    g = Graph([_row(1, "Nowa platforma: pasywny dochód 5000 zł"), _row(2, "Przepis na sernik platforma")])
    g.rows[1]["ad_creative_bodies"] = ["Przepis na sernik"]
    r = job_adlibrary_pull(s, None, wl, http_get=g, sleep=lambda x: None,
                           enricher=Enricher(cache_dir=tmp_path / "enr", live=True), today=date(2026, 11, 21))
    assert r["new"] == 1 and r["not_flagged"] == 1
    meta = json.loads((sched_dir(s) / "inbox" / "adlib-8100000000001" / "adlibrary.json").read_text())
    assert "keyword:pasywny dochód" in meta["reasons"]
    assert not (sched_dir(s) / "inbox" / "adlib-8100000000002").exists()
    assert al.token_configured()


def test_inbox_splits_ad_library_export(settings, monkeypatch):
    import hydra.pipeline as pl

    monkeypatch.setattr(pl, "analyse_input", lambda *a, **k: pytest.fail("an ad export is not analysed whole"))
    inbox = sched_dir(settings) / "inbox"
    csv = inbox / "ad_library_export.csv"
    csv.write_text("Library ID,Page name,Ad creative body,Link URL\n"
                   "8200000000001,Fikcyjny Portal,Wywiad usunięty! Zarabiaj z domu,https://lure-a.example/\n"
                   "8200000000002,Fikcyjny Portal,Inna reklama,https://lure-b.example/\n", encoding="utf-8")
    old = time.time() - 60
    os.utime(csv, (old, old))
    r = job_inbox(settings, None, load_watchlist(settings))
    assert r["items"] == [{"item": "ad_library_export.csv", "ad_library_export": 2}]
    meta = json.loads((inbox / "adlib-8200000000001" / "adlibrary.json").read_text())
    assert any(x.startswith("keyword:") for x in meta["reasons"])
    assert (inbox / "adlib-8200000000002" / "ad_text.txt").read_text() == "Inna reklama"
    assert any(p.name.endswith("ad_library_export.csv") for p in (sched_dir(settings) / "processed").iterdir())
