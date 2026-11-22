"""Hardened scheduled ad-library scan (ADOPT item 10): paging, retries, resume cursor, watermark, exact phrase,
page-id sweep, dedup, kill switch, replay, snapshot removal status, token hygiene. Synthetic data, mocked HTTP."""

from __future__ import annotations

import json
from datetime import date

import pytest

from hydra.config import Settings
from hydra.enrich import adlibrary as al
from hydra.enrich.client import Enricher
from hydra.scheduler import job_adlibrary_pull, load_watchlist, run_due, sched_dir, watch_add


@pytest.fixture(autouse=True)
def _store_every_ad(monkeypatch):
    """These tests cover paging/retry/dedup: every collected ad is stored (rule filter: test_adlib_hint.py)."""
    import hydra.scheduler as sch

    real = sch.adlib_rules

    def lenient(wl):
        r = real(wl)
        r.min_reasons, r.max_domain_age_days = 0, None
        return r

    monkeypatch.setattr(sch, "adlib_rules", lenient)


TOKEN = "EAAhardTOKEN9876543210zyxwvuZYXWVU"
KW = "szybki zysk"
QID = f"kw:{KW}"


def _ad(n: int, body: str | None = None, page: str = "9000000001") -> dict:
    return {
        "id": f"7000000000{n:03d}",
        "page_id": page,
        "page_name": "Fikcyjna Strona",
        "ad_creative_bodies": [body or f"Oferta numer {n} na lure-{n}.example"],
        "ad_creative_link_urls": [f"https://lure-{n}.example/?sub1=x"],
        "ad_delivery_start_time": "2026-11-01",
        "ad_snapshot_url": f"https://www.facebook.com/ads/archive/render_ad/?id={n}&access_token={TOKEN}",
    }


class Graph:
    """Mocked ads_archive for keyword queries: page i answers `after=C<i>`; `fail[i]` = number of 500s before
    page i succeeds. Page-id sweeps get an empty result."""

    def __init__(self, pages: list[list[dict]], fail: dict[int, int] | None = None):
        self.pages, self.fail, self.calls = pages, dict(fail or {}), []

    def kw_calls(self) -> list[dict]:
        return [c for c in self.calls if "search_terms" in c]

    def __call__(self, url, params):
        assert params["access_token"] == TOKEN
        self.calls.append({k: v for k, v in params.items() if k != "access_token"})
        if "search_page_ids" in params:
            return 200, {"data": []}
        i = int(params["after"][1:]) if "after" in params else 0
        if self.fail.get(i, 0) > 0:
            self.fail[i] -= 1
            return 500, {"error": {"message": f"Internal error access_token={TOKEN}", "code": 2}}
        body: dict = {"data": self.pages[i]}
        if i + 1 < len(self.pages):
            nxt = f"https://graph.facebook.com/v21.0/ads_archive?after=C{i + 1}&access_token={TOKEN}"
            body["paging"] = {"cursors": {"after": f"C{i + 1}"}, "next": nxt}
        return 200, body


def _never(*_a):
    pytest.fail("network call while the kill switch is on")


@pytest.fixture
def live(settings: Settings, monkeypatch) -> Settings:
    monkeypatch.setenv("HYDRA_META_TOKEN", TOKEN)
    monkeypatch.setitem(al.DELAY, al.SOURCE, 0)
    return settings.model_copy(update={"enrich": True, "offline": False})


def _token_absent(root) -> None:
    files = [f for f in root.rglob("*") if f.is_file()]
    assert files
    for f in files:
        assert TOKEN not in f.read_text(errors="replace"), f


def _inbox_ids(s: Settings) -> set[str]:
    return {p.name.removeprefix("adlib-") for p in (sched_dir(s) / "inbox").iterdir() if p.name.startswith("adlib-")}


def test_acceptance_paging_retry_resume_watermark(live: Settings, tmp_path, monkeypatch):
    watch_add(live, keywords=[KW])
    wl = load_watchlist(live)
    # run 1: 3 pages, page 2 (index 1) fails once with 500 and repeats ad 2; ad 5 repeats ad 1's page + body
    dup_body = "  OFERTA numer 1 na LURE-1.example "
    g = Graph([[_ad(1), _ad(2)], [_ad(3), _ad(2)], [_ad(4), _ad(5, body=dup_body)]], fail={1: 1})
    sleeps: list[float] = []
    r = job_adlibrary_pull(live, None, wl, http_get=g, sleep=sleeps.append, today=date(2026, 11, 20))
    assert (r["status"], r["new"], r["mode"]) == ("ok", 4, "live")
    assert sleeps == [1.0] and len(g.calls) == 4
    assert r["dedup"] == {"ad_id": 1, "page_body": 1}
    assert _inbox_ids(live) == {f"7000000000{n:03d}" for n in (1, 2, 3, 4)}
    first = g.calls[0]
    assert first["search_type"] == "KEYWORD_EXACT_PHRASE" and first["limit"] == "100"
    assert "ad_delivery_date_min" not in first and "after" not in first
    assert [c.get("after") for c in g.calls] == [None, "C1", "C1", "C2"]
    meta = json.loads((sched_dir(live) / "inbox" / "adlib-7000000000001" / "adlibrary.json").read_text())
    assert meta["keyword"] == KW and meta["search_type"] == "KEYWORD_EXACT_PHRASE" and "moderation" not in meta
    d = sched_dir(live) / "adlibrary"
    assert json.loads((d / "watermarks.json").read_text())[QID]["last_success"] == "2026-11-20"

    # run 2: watermark - 2 days; page 2 fails 3x -> partial, resume cursor stored, watermark unchanged
    g2 = Graph([[_ad(6)], [_ad(7)], [_ad(8)]], fail={1: 3})
    sleeps.clear()
    r2 = job_adlibrary_pull(live, None, wl, http_get=g2, sleep=sleeps.append, today=date(2026, 11, 21))
    assert r2["status"] == "partial" and r2["new"] == 1 and sleeps == [1.0, 2.0, 4.0]
    assert g2.calls[0]["ad_delivery_date_min"] == "2026-11-18"
    cur = json.loads((d / "cursor.json").read_text())[QID]
    assert cur == {"page": 1, "after": "C1", "date_min": "2026-11-18"}
    assert r2["queries"][QID]["error"].startswith("error:http500")
    assert json.loads((d / "watermarks.json").read_text())[QID]["last_success"] == "2026-11-20"

    # run 3: resumes at page 2 with the same date window, no page-1 refetch; completes, cursor cleared
    n_before = len(g2.kw_calls())
    r3 = job_adlibrary_pull(live, None, wl, http_get=g2, sleep=sleeps.append, today=date(2026, 11, 21))
    resumed = g2.kw_calls()[n_before:]
    assert [c.get("after") for c in resumed] == ["C1", "C2"]
    assert all(c["ad_delivery_date_min"] == "2026-11-18" for c in resumed)
    assert (r3["status"], r3["new"]) == ("ok", 2) and r3["queries"][QID]["resumed_from"]["page"] == 1
    assert json.loads((d / "cursor.json").read_text()) == {}
    assert _inbox_ids(live) >= {"7000000000006", "7000000000007", "7000000000008"}

    # run 4 through run_due (default http layer patched): watermark 2026-11-21 - 2 days; status.json token-free
    g4 = Graph([[]])
    monkeypatch.setattr(al, "_urllib_get", g4)
    st = run_due(live, None, force=True, only=["adlibrary_pull"])
    assert st["jobs"]["adlibrary_pull"]["status"] == "ok"
    assert g4.calls[0]["ad_delivery_date_min"] == "2026-11-19"
    _token_absent(tmp_path)


def test_kill_switch_makes_no_calls_and_replay_still_works(live: Settings, settings: Settings):
    watch_add(live, keywords=["inwestycja"])
    wl = load_watchlist(live)
    for off in (settings.model_copy(update={"enrich": True, "offline": True}),
                settings.model_copy(update={"enrich": False, "offline": False})):
        r = job_adlibrary_pull(off, None, wl, http_get=_never, sleep=_never,
                               enricher=Enricher(cache_dir=off.enrich_cache, live=True))  # kill switch wins
        assert (r["status"], r["mode"]) == ("unavailable", "replay") and "HYDRA_OFFLINE" in r["note"]
    g = Graph([[_ad(1)], [_ad(2)]])
    r = job_adlibrary_pull(live, None, wl, http_get=g, sleep=_never, today=date(2026, 11, 20))
    assert r["queries"]["kw:inwestycja"]["search_type"] == "KEYWORD_UNORDERED" and r["new"] == 2
    off = settings.model_copy(update={"enrich": True, "offline": True})
    r2 = job_adlibrary_pull(off, None, wl, http_get=_never, sleep=_never)
    q = r2["queries"]["kw:inwestycja"]
    assert (r2["status"], r2["mode"], q["pages"], q["ads"], r2["new"]) == ("ok", "replay", 2, 2, 0)


def test_no_token_is_unavailable_with_polish_note(live: Settings, monkeypatch):
    monkeypatch.delenv("HYDRA_META_TOKEN")
    watch_add(live, keywords=["inwestycja"])
    r = job_adlibrary_pull(live, None, load_watchlist(live), http_get=_never, sleep=_never)
    assert r["status"] == "unavailable" and "HYDRA_META_TOKEN" in r["note"] and "inbox/" in r["note"]


def test_replay_of_cached_pages_gives_identical_ads(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)
    monkeypatch.setenv("HYDRA_META_TOKEN", TOKEN)
    monkeypatch.setitem(al.DELAY, al.SOURCE, 0)
    params = al.ads_archive_params(al.Rules(date_min="2026-11-01"), term="Inwestycja")
    g = Graph([[_ad(1), _ad(2)], [_ad(3)], [_ad(4)]])
    a = al.page_through(params, enricher=Enricher(cache_dir=tmp_path, live=True), http_get=g, sleep=_never)
    monkeypatch.delenv("HYDRA_META_TOKEN")
    b = al.page_through(params, enricher=Enricher(cache_dir=tmp_path, live=False), http_get=_never, sleep=_never)
    assert a.status == b.status == "ok" and a.pages == b.pages == 3
    assert [x.ad_id for x in a.ads] == [x.ad_id for x in b.ads] and len(a.ads) == 4
    # query strings are case-sensitive cache keys (Linux CI): a different case is a different key, never a hit
    other = al.ads_archive_params(al.Rules(date_min="2026-11-01"), term="inwestycja")
    assert al._query_key(other) != al._query_key(params)
    assert al.page_through(other, enricher=Enricher(cache_dir=tmp_path, live=False)).status == "unavailable"
    _token_absent(tmp_path)


def test_retry_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # live=True alone never overrides the kill switch (SEC-FIX)
    monkeypatch.setenv("HYDRA_META_TOKEN", TOKEN)
    monkeypatch.setitem(al.DELAY, al.SOURCE, 0)
    params = al.ads_archive_params(al.Rules(), term="x")
    # Graph throttling code on HTTP 400 and a transport exception are retried
    calls = iter([(400, {"error": {"code": 17, "message": "User request limit reached"}}), ConnectionError(TOKEN)])

    def flaky(url, p):
        nxt = next(calls, None)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt or (200, {"data": [_ad(1)]})

    sleeps: list[float] = []
    enr = Enricher(cache_dir=tmp_path, live=True)
    run = al.page_through(params, enricher=enr, http_get=flaky, sleep=sleeps.append)
    assert run.status == "ok" and sleeps == [1.0, 2.0] and [a.ad_id for a in run.ads] == ["7000000000001"]
    # invalid token (code 190) is final: no retry, partial with a resume cursor at page 0
    bad = al.page_through(al.ads_archive_params(al.Rules(), term="y"), enricher=enr, sleep=_never,
                          http_get=lambda u, p: (400, {"error": {"code": 190, "message": f"bad {TOKEN}"}}))
    assert bad.status == "partial" and bad.resume == {"page": 0, "after": None} and TOKEN not in bad.error
    _token_absent(tmp_path)


def test_page_id_sweep_watchlist_and_flagged_pages(live: Settings):
    watch_add(live, keywords=["bonus"], page_ids=["9000000777"])
    wl = load_watchlist(live)
    assert wl["page_ids"] == ["9000000777"]
    g = Graph([[_ad(1, page="9000000222")]])
    job_adlibrary_pull(live, None, wl, http_get=g, sleep=_never, today=date(2026, 11, 20))
    sweep = [c for c in g.calls if "search_page_ids" in c]
    assert sweep and sweep[0]["search_page_ids"] == "9000000777" and "search_terms" not in sweep[0]
    # next run: the page of the ad stored in run 1 joins the sweep
    g2 = Graph([[]])
    r = job_adlibrary_pull(live, None, wl, http_get=g2, sleep=_never, today=date(2026, 11, 21))
    assert [c["search_page_ids"] for c in g2.calls if "search_page_ids" in c] == ["9000000777,9000000222"]
    assert "pages:9000000777,9000000222" in r["queries"]
    # more than 10 ids are split into batches of <= 10
    watch_add(live, page_ids=[f"91{i:08d}" for i in range(12)])
    g3 = Graph([[]])
    job_adlibrary_pull(live, None, load_watchlist(live), http_get=g3, sleep=_never, today=date(2026, 11, 22))
    batches = [c["search_page_ids"].split(",") for c in g3.calls if "search_page_ids" in c]
    assert len(batches) == 2 and all(len(b) <= 10 for b in batches) and sum(map(len, batches)) == 14


REMOVED_EN = ("<html><body><div>This ad was taken down because it didn't follow our "
              "<a href='#'>Advertising Standards</a>.</div></body></html>")
REMOVED_PL = ("<html><body><p>Usunęliśmy tę reklamę, ponieważ nie była zgodna z naszymi "
              "Standardami reklamowymi.</p></body></html>")
TEXT_ONLY = "<html><body><p>Fikcyjna oferta: zarobek bez ryzyka na lure-9.example</p></body></html>"


def test_snapshot_status_and_moderation_fact(live: Settings):
    assert al.snapshot_status(REMOVED_EN) == "removed_policy_violation"
    assert al.snapshot_status(REMOVED_PL) == "removed_policy_violation"
    assert al.snapshot_status(TEXT_ONLY) == "no_media"
    assert al.snapshot_status("<p>Removed from cart.</p>") == "no_media"  # removal alone is not enough
    watch_add(live, keywords=["bonus"])
    snaps = sched_dir(live) / "adlibrary" / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "7000000000001.html").write_text(REMOVED_PL, encoding="utf-8")
    (snaps / "7000000000002.html").write_text(TEXT_ONLY, encoding="utf-8")
    r = job_adlibrary_pull(live, None, load_watchlist(live), http_get=Graph([[_ad(1), _ad(2)]]), sleep=_never)
    inbox = sched_dir(live) / "inbox"
    assert json.loads((inbox / "adlib-7000000000001" / "adlibrary.json").read_text())["moderation"] == (
        "removed_policy_violation")
    assert "moderation" not in json.loads((inbox / "adlib-7000000000002" / "adlibrary.json").read_text())
    assert r["removed_policy_violation"] == ["7000000000001"]
