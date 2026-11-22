"""Bonus (task §3): watchlist + periodic jobs; status JSON for the UI (`GET /v1/scheduler/status`).

State lives in `<state_dir>/scheduler/`: `watchlist.json` (keywords, domains, intervals), `status.json` (per
job: last start/finish, status, detail, next due), `inbox/` (drop files, folders or ZIPs here; manual import
path) and `processed/`.

Jobs:
- `inbox`: every new item in `inbox/` → `pipeline.analyse_input("file")` → assignment; moved to `processed/`.
- `keyword_watch`: each watchlist keyword → `pipeline.search_hint` over the corpus; new hits are reported.
- `reenrich_domains`: watchlist domains + hosts of the latest run → `hydra.enrich` RDAP/DNS lookups (only when
  `live_enabled`: HYDRA_ENRICH=1 and HYDRA_OFFLINE=0; otherwise skipped, replay-only).
- `adlibrary_pull`: Meta Ad Library API (`ads_archive`, country PL) for watchlist keywords (multi-word = exact
  phrase) and a page-id sweep (watchlist `page_ids` + pages of ads stored earlier), through
  `hydra.enrich.adlibrary.page_through`: up to 5 pages of 100 per query, each page cached (replayable), 3 attempts
  per page (1/2/4 s), resume cursor in `adlibrary/cursor.json`, per-query watermark in `adlibrary/watermarks.json`
  (next `ad_delivery_date_min` = last complete run - 2 days), dedup by ad id and (page id, body hash) in
  `adlibrary/seen.json`. Live needs `HYDRA_META_TOKEN` and live enrichment; without them the last complete run is
  replayed from the cache (no network). No token and no cache: `unavailable` (new material via `inbox/`).
  Each new ad becomes an incident folder (ad text + adlibrary.json) dropped into `inbox/`. Saved snapshot pages in
  `adlibrary/snapshots/<ad_id>.html` give `moderation: removed_policy_violation` (never fetched, never a gene).

All writes go through the caller's `Writer` (the API passes its own; never a second writer in a process).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.redact import redact_text

DEFAULT_WATCHLIST: dict[str, Any] = {
    "keywords": [], "domains": [], "page_ids": [],
    "intervals_s": {"inbox": 60, "keyword_watch": 3600, "reenrich_domains": 86400, "adlibrary_pull": 21600},
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sched_dir(settings: Settings) -> Path:
    assert settings.state_dir is not None
    d = settings.state_dir / "scheduler"
    for sub in ("inbox", "processed", "failed", "adlibrary"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def load_watchlist(settings: Settings) -> dict[str, Any]:
    p = sched_dir(settings) / "watchlist.json"
    wl = json.loads(json.dumps(DEFAULT_WATCHLIST))
    if p.is_file():
        stored = json.loads(p.read_text(encoding="utf-8"))
        wl.update({k: v for k, v in stored.items() if k != "intervals_s"})
        wl["intervals_s"].update(stored.get("intervals_s", {}))
    return wl


def save_watchlist(settings: Settings, wl: dict[str, Any]) -> None:
    (sched_dir(settings) / "watchlist.json").write_text(json.dumps(wl, ensure_ascii=False, indent=2), "utf-8")


def watch_add(settings: Settings, *, keywords: list[str] = (), domains: list[str] = (),
              page_ids: list[str] = ()) -> dict[str, Any]:
    wl = load_watchlist(settings)
    wl["keywords"] = sorted(set(wl["keywords"]) | {k.strip() for k in keywords if k.strip()})
    wl["domains"] = sorted(set(wl["domains"]) | {d.strip().lower() for d in domains if d.strip()})
    wl["page_ids"] = sorted(set(wl.get("page_ids", [])) | {str(p).strip() for p in page_ids if str(p).strip()})
    save_watchlist(settings, wl)
    return wl


def scheduler_status(settings: Settings) -> dict[str, Any]:
    p = sched_dir(settings) / "status.json"
    st = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"jobs": {}}
    st["watchlist"] = load_watchlist(settings)
    from hydra.enrich.adlibrary import token_configured

    st["meta_token_configured"] = token_configured()
    from hydra.enrich.client import live_enabled

    st["live_enrichment"] = live_enabled(settings)
    st["running"] = _RUN_LOCK.locked()
    st["inbox_pending"] = len([x for x in (sched_dir(settings) / "inbox").iterdir() if not x.name.startswith(".")])
    return st


def _save_status(settings: Settings, st: dict[str, Any]) -> None:
    st = {k: v for k, v in st.items() if k == "jobs"}
    tmp = sched_dir(settings) / "status.json.tmp"
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, sched_dir(settings) / "status.json")


# --------------------------------------------------------------------------------------- jobs


def job_inbox(settings: Settings, writer: Writer, wl: dict[str, Any]) -> dict[str, Any]:
    from hydra.pipeline import analyse_input

    d = sched_dir(settings)
    results, waiting = [], 0
    now = time.time()
    for item in sorted((d / "inbox").iterdir()):
        if item.name.startswith("."):
            continue
        if now - _latest_mtime(item) < SETTLE_S:
            waiting += 1  # still being copied in; picked up on the next run
            continue
        ok = True
        try:
            n_ads = _split_ad_export(settings, item, wl)
            if n_ads is not None:  # Ad Library CSV/JSON export: one inbox folder per ad, analysed next tick
                results.append({"item": item.name, "ad_library_export": n_ads})
                shutil.move(str(item), d / "processed" / f"{time.strftime('%Y%m%dT%H%M%S')}-{item.name}")
                continue
            res = analyse_input("file", item, settings, writer=writer)
            a = res["assignment"]
            results.append({"item": item.name, "incident_id": res["incident_id"], "decision": a["decision"],
                            "campaign_id": a.get("campaign_id"), "group_id": a.get("group_id")})
        except Exception as exc:  # noqa: BLE001 — one bad item never blocks the inbox
            ok = False
            results.append({"item": item.name, "error": f"{type(exc).__name__}: {exc}"[:300]})
        dest = d / ("processed" if ok else "failed") / f"{time.strftime('%Y%m%dT%H%M%S')}-{item.name}"
        shutil.move(str(item), dest)
    return {"status": "ok", "processed": len(results), "waiting": waiting,
            "failed": sum(1 for r in results if "error" in r), "items": results}


SETTLE_S = 10.0


def _split_ad_export(settings: Settings, item: Path, wl: dict[str, Any]) -> int | None:
    """A downloaded Ad Library export (CSV/TSV, or API-shaped JSON with ad rows) dropped into `inbox/` becomes one
    `adlib-<id>` folder per new ad (rule reasons recorded; the analyst chose the export, so nothing is filtered).
    None when the file is not an ad export (then it is analysed as ordinary material)."""
    from hydra.enrich import adlibrary as al

    low = item.name.lower()
    if not item.is_file() or not low.endswith((".csv", ".tsv", ".json")) or low.endswith(".info.json"):
        return None
    ads = [a for a in al.import_file(item) if a.bodies or a.page_name or a.snapshot_url or a.link_urls]
    if not ads:
        return None
    d = sched_dir(settings) / "adlibrary"
    seen = _read_json(d / "seen.json", {})
    seen = {"ad_ids": seen} if isinstance(seen, list) else seen
    ids = set(seen.get("ad_ids", []))
    from hydra.enrich.client import Enricher

    rules, enr, n = adlib_rules(wl), Enricher(settings=settings), 0
    for ad in ads:
        if ad.ad_id in ids:
            continue
        ids.add(ad.ad_id)
        ad.reasons = al.evaluate(ad, rules, enricher=enr)
        _store_ad(sched_dir(settings) / "inbox", ad, {"term": None, "export": item.name}, None, ad.moderation)
        n += 1
    _write_json(d / "seen.json", {**seen, "ad_ids": sorted(ids)})
    return n


def _latest_mtime(p: Path) -> float:
    if p.is_file():
        return p.stat().st_mtime
    times = [f.stat().st_mtime for f in p.rglob("*") if f.is_file()]
    return max(times, default=p.stat().st_mtime)


def job_keyword_watch(settings: Settings, writer: Writer, wl: dict[str, Any]) -> dict[str, Any]:
    from hydra.pipeline import search_hint

    if not wl["keywords"]:
        return {"status": "idle", "note": "watchlist has no keywords"}
    hits = {}
    for kw in wl["keywords"]:
        r = search_hint(kw, settings)
        hits[kw] = [{"incident_id": i["incident_id"], "score": i["score"]} for i in r["incidents"][:10]]
    return {"status": "ok", "hits": hits}


def _corpus_hosts(settings: Settings, limit: int = 200) -> list[str]:
    if not settings.db_path.exists():
        return []
    with closing(connect(settings.db_path, readonly=True)) as conn:
        rows = conn.execute("SELECT DISTINCT value FROM genes WHERE status='observed' AND value IS NOT NULL AND"
                            " kind IN ('registrable_domain', 'landing_host')"
                            " LIMIT ?", (limit,)).fetchall()
    return [r["value"] for r in rows]


def job_reenrich_domains(settings: Settings, writer: Writer, wl: dict[str, Any]) -> dict[str, Any]:
    from hydra.enrich.client import live_enabled

    domains = sorted(set(wl["domains"]) | set(_corpus_hosts(settings)))
    if not live_enabled(settings):
        return {"status": "skipped", "note": "HYDRA_OFFLINE=1 or HYDRA_ENRICH=0: replay-only, no live lookups",
                "domains": len(domains)}
    from hydra.enrich import dns, get_enricher, rdap

    enr = get_enricher(None, settings)
    done, failed = 0, 0
    for d in domains:
        try:
            rdap.domain(enr, d)
            dns.query(enr, d, "A")
            done += 1
        except Exception:  # noqa: BLE001 — a failed lookup is "unavailable", never an error
            failed += 1
    return {"status": "ok", "domains": len(domains), "refreshed": done, "failed": failed}


ADLIB_LOOKBACK_DAYS = 2  # next run: ad_delivery_date_min = last complete run - 2 days (late-indexed ads)
ADLIB_MAX_SWEEP_PAGES = 50  # page ids per page-id sweep (5 requests of 10)
NOTE_NO_TOKEN_PL = ("Brak tokenu (META_AD_LIBRARY_TOKEN, HYDRA_META_TOKEN lub ~/.config/hydra/meta.env): "
                    "Meta Ad Library API wymaga tokenu użytkownika (konto Meta for "
                    "Developers + weryfikacja tożsamości). Import ręczny przez inbox/ (CSV/JSON, zapisana strona, "
                    "zrzut ekranu) działa dalej.")
NOTE_OFFLINE_PL = ("Tryb offline (HYDRA_OFFLINE=1 lub HYDRA_ENRICH=0): bez połączeń sieciowych; tylko odtworzenie "
                   "z pamięci podręcznej, która dla tych zapytań jest pusta.")


def _read_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
    except ValueError:
        return default


def _write_json(p: Path, obj: Any) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def adlib_rules(wl: dict[str, Any]) -> Any:
    """Rule engine settings for scheduled ads (`adlibrary.evaluate`): the default scam lexicon + watchlist keywords,
    watchlist celebrities / invented platform names, watchlist domains as known campaign hosts, an optional
    delivery-date window (`ad_date_min`/`ad_date_max`), new-domain age (`max_domain_age_days`, default 30, RDAP)
    and `adlib_min_reasons` (default 1). Ads below the threshold are counted, remembered as seen, not stored."""
    from hydra.enrich import adlibrary as al

    age = wl.get("max_domain_age_days", 30)
    return al.Rules(
        keywords=[*al.DEFAULT_KEYWORDS, *[k for k in wl.get("keywords", []) if k.strip()]],
        celebrities=list(wl.get("celebrities", [])), platform_names=list(wl.get("platform_names", [])),
        known_hosts={d.lower() for d in wl.get("domains", [])}, date_min=wl.get("ad_date_min"),
        date_max=wl.get("ad_date_max"), max_domain_age_days=int(age) if age is not None else None,
        min_reasons=int(wl.get("adlib_min_reasons", 1)))


def job_adlibrary_pull(settings: Settings, writer: Writer | None, wl: dict[str, Any], *,
                       http_get: Callable | None = None, sleep: Callable[[float], None] = time.sleep,
                       enricher: Any = None, today: date | None = None) -> dict[str, Any]:
    """Keyword sweep + page-id sweep of ads_archive through `adlibrary.page_through` (per-page replay cache,
    3 attempts per page with 1/2/4 s backoff, resume cursor, date watermark, dedup by ad id and page+body hash).
    New ads become inbox folders. Live only with a token and live enrichment (HYDRA_ENRICH=1, HYDRA_OFFLINE=0);
    otherwise the last complete run is replayed from the cache and no network call is made."""
    from hydra.enrich import adlibrary as al
    from hydra.enrich.client import Enricher, live_enabled

    d = sched_dir(settings) / "adlibrary"
    seen = _read_json(d / "seen.json", {})
    seen = {"ad_ids": seen} if isinstance(seen, list) else seen  # legacy: plain list of ad ids
    ad_ids, bodies = set(seen.get("ad_ids", [])), set(seen.get("body_keys", []))
    flagged_pages = set(seen.get("page_ids", []))
    keywords = [k.strip() for k in wl.get("keywords", []) if k.strip()]
    watch_pages = sorted({str(p).strip() for p in wl.get("page_ids", []) if str(p).strip()})
    sweep = (watch_pages + sorted(flagged_pages - set(watch_pages)))[:ADLIB_MAX_SWEEP_PAGES]
    if not keywords and not sweep:
        return {"status": "idle", "note": "watchlist has no keywords or page ids"}
    token = al._token()
    enr = enricher or Enricher(settings=settings, max_age_s=float(wl["intervals_s"].get("adlibrary_pull", 21600)) / 2)
    if not live_enabled(settings):
        enr.live = False  # HYDRA_OFFLINE=1 / HYDRA_ENRICH=0 is a hard kill switch, whatever the caller passed
    live = bool(enr.live and token)
    marks, cursors = _read_json(d / "watermarks.json", {}), _read_json(d / "cursor.json", {})
    moderation = _snapshot_moderation(d, al)
    today = today or datetime.now(UTC).date()
    queries = [(f"kw:{k}", {"term": k}) for k in keywords] + [
        (f"pages:{','.join(b)}", {"page_ids": b})
        for b in (sweep[i:i + al.PAGE_ID_BATCH] for i in range(0, len(sweep), al.PAGE_ID_BATCH))]
    out: dict[str, Any] = {}
    new = dup_id = dup_body = not_flagged = 0
    judge = adlib_rules(wl)
    for qid, q in queries:
        cur, mark = cursors.get(qid) if live else None, marks.get(qid) or {}
        if cur:
            date_min, start, after = cur.get("date_min"), int(cur.get("page", 0)), cur.get("after")
        elif live and mark.get("last_success"):
            date_min = (date.fromisoformat(mark["last_success"]) - timedelta(days=ADLIB_LOOKBACK_DAYS)).isoformat()
            start, after = 0, None
        else:  # first live run, or replay of the last complete run's exact queries
            date_min, start, after = mark.get("date_min"), 0, None
        rules = al.Rules(search_terms=[q["term"]] if "term" in q else [], date_min=date_min)
        params = al.ads_archive_params(rules, term=q.get("term"), page_ids=q.get("page_ids", ()))
        run = al.page_through(params, enricher=enr, http_get=http_get, sleep=sleep, max_pages=rules.max_pages,
                              start_page=start, after=after)
        q_new = 0
        for ad in run.ads:
            bk = al.body_key(ad.page_id, ad.bodies)
            if ad.ad_id in ad_ids:
                dup_id += 1
                continue
            ad_ids.add(ad.ad_id)
            if bk and bk in bodies:
                dup_body += 1
                continue
            bodies.update([bk] if bk else [])
            ad.reasons = al.evaluate(ad, judge, enricher=enr)  # named rule reasons (keywords, hosts, dates, age)
            if len(ad.reasons) < judge.min_reasons:
                not_flagged += 1
                continue
            flagged_pages.update([str(ad.page_id)] if ad.page_id else [])
            _store_ad(sched_dir(settings) / "inbox", ad, q, params.get("search_type"), moderation.get(ad.ad_id))
            q_new += 1
        new += q_new
        if live and run.status == "ok":
            marks[qid] = {"last_success": today.isoformat(), "date_min": date_min}
            cursors.pop(qid, None)
        elif live and run.resume is not None:
            cursors[qid] = {**run.resume, "date_min": date_min}
        out[qid] = {"status": run.status, "pages": run.pages, "ads": len(run.ads), "new": q_new,
                    "date_min": date_min, "search_type": params.get("search_type"), "resumed_from": cur,
                    **({"error": run.error} if run.error else {}),
                    **({"resume": cursors[qid]} if live and run.resume else {})}
    _write_json(d / "seen.json", {"ad_ids": sorted(ad_ids), "body_keys": sorted(bodies),
                                  "page_ids": sorted(flagged_pages)})
    if live:
        _write_json(d / "watermarks.json", marks)
        _write_json(d / "cursor.json", cursors)
    sts = {v["status"] for v in out.values()}
    status = "partial" if "partial" in sts else "unavailable" if sts == {"unavailable"} else "ok"
    r: dict[str, Any] = {"status": status, "new": new, "mode": "live" if live else "replay",
                         "dedup": {"ad_id": dup_id, "page_body": dup_body}, "not_flagged": not_flagged,
                         "queries": out,
                         "removed_policy_violation": sorted(k for k, v in moderation.items()
                                                            if v == "removed_policy_violation")}
    if not token:
        r["note"] = NOTE_NO_TOKEN_PL
    elif status == "unavailable" and not live:
        r["note"] = NOTE_OFFLINE_PL
    return r


def _snapshot_moderation(d: Path, al: Any) -> dict[str, str]:
    """Snapshot pages the analyst saved for already flagged ads (`adlibrary/snapshots/<ad_id>.html`; never crawled):
    ad id -> snapshot status. A removal is written into the ad's inbox folder as a platform-moderation fact."""
    snaps = d / "snapshots"
    snaps.mkdir(exist_ok=True)
    res = {p.stem: al.snapshot_status(p.read_text(encoding="utf-8", errors="replace")) for p in snaps.glob("*.htm*")}
    for ad_id, st in res.items():
        meta_p = d.parent / "inbox" / f"adlib-{ad_id}" / "adlibrary.json"
        meta = _read_json(meta_p, None)
        if st == "removed_policy_violation" and isinstance(meta, dict) and meta.get("moderation") != st:
            _write_json(meta_p, meta | {"moderation": st})
    _write_json(d / "moderation.json", res)
    return res


def _store_ad(inbox: Path, ad: Any, q: dict[str, Any], search_type: str | None, moderation: str | None) -> None:
    from hydra.redact import redact_url

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ad.ad_id)[:60]
    folder = inbox / f"adlib-{safe}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ad_text.txt").write_text(redact_text("\n\n".join(ad.bodies)) or "", encoding="utf-8")
    meta = {"id": ad.ad_id, "page_id": ad.page_id, "page_name": ad.page_name,
            "ad_delivery_start_time": ad.delivery_start, "ad_delivery_stop_time": ad.delivery_stop,
            "ad_snapshot_url": redact_url(ad.snapshot_url), "link_urls": [redact_url(u) for u in ad.link_urls],
            "publisher_platforms": ad.platforms, "keyword": q.get("term"), "search_page_ids": q.get("page_ids"),
            "search_type": search_type, "fetched_at": ad.fetched_at, "enrichment_key": ad.enrichment_key,
            "reasons": list(ad.reasons or [])}
    if moderation == "removed_policy_violation":
        meta["moderation"] = moderation
    (folder / "adlibrary.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def meta_collector_owns(settings: Settings) -> bool:
    """META-COLLECT owns Meta polling once its configuration has a query (hydra/meta): the scheduler then never runs
    a second, independent collector with its own budget."""
    if not settings.db_path.exists():
        return False
    try:
        with closing(connect(settings.db_path, readonly=True)) as conn:
            return bool(conn.execute("SELECT 1 FROM meta_queries WHERE origin IN ('manual', 'scheduler', 'expansion') "
                                     "LIMIT 1").fetchone())
    except Exception:  # noqa: BLE001 — no v8 tables yet: the legacy job stays in charge
        return False


def job_adlibrary_scheduled(settings: Settings, writer: Writer | None, wl: dict[str, Any]) -> dict[str, Any]:
    """Scheduler entry for `adlibrary_pull`. With META-COLLECT configured it delegates: one cycle of the single Meta
    collector (same lock, HYDRA budget, cursors and analysis outbox; `hydra.meta.collector.run_cycle`). Otherwise
    the legacy job runs, its HTTP also through the shared Meta transport and budget."""
    if writer is not None and meta_collector_owns(settings):
        from hydra.meta.collector import run_cycle

        res = run_cycle(settings, writer, trigger="scheduler")
        out = res.to_dict()
        out.update(delegated=True, mode="live", new=res.new_revisions,
                   note="Delegowano do kolektora META-COLLECT (WATCH-RT, jeden budżet i jedna kolejka analiz).")
        out["status"] = {"empty": "ok", "deferred_budget": "deferred", "needs_config": "unavailable",
                         "offline": "unavailable"}.get(res.status, res.status)
        return out
    return job_adlibrary_pull(settings, writer, wl)


JOBS: dict[str, Callable[..., dict[str, Any]]] = {
    "inbox": job_inbox, "keyword_watch": job_keyword_watch, "reenrich_domains": job_reenrich_domains,
    "adlibrary_pull": job_adlibrary_scheduled,
}


_RUN_LOCK = threading.Lock()  # one run at a time: background loop and "Uruchom teraz" never overlap


def run_now_background(settings: Settings, writer: Writer, only: list[str] | None = None) -> bool:
    """"Uruchom teraz" (API `POST /v1/scheduler/run`): every job once, forced, in a daemon thread with the
    caller's Writer. False when a run is already in progress."""
    if _RUN_LOCK.locked():
        return False

    def _go() -> None:
        try:
            run_due(settings, writer, force=True, only=only)
        except Exception:  # noqa: BLE001 — job failures are recorded per job; never crash the API
            pass

    threading.Thread(target=_go, name="hydra-scheduler-now", daemon=True).start()
    return True


def run_due(settings: Settings, writer: Writer, *, force: bool = False, only: list[str] | None = None
            ) -> dict[str, Any]:
    """Run every job whose interval has elapsed (or all with `force`). Returns the new status."""
    with _RUN_LOCK:
        return _run_due(settings, writer, force=force, only=only)


def _run_due(settings: Settings, writer: Writer, *, force: bool, only: list[str] | None) -> dict[str, Any]:
    wl = load_watchlist(settings)
    st = scheduler_status(settings)
    now = time.time()
    for name, fn in JOBS.items():
        if only and name not in only:
            continue
        js = st["jobs"].get(name, {})
        if not force and js.get("next_due_ts", 0) > now:
            continue
        started = _now()
        try:
            detail = fn(settings, writer, wl)
            status = detail.get("status", "ok")
        except Exception as exc:  # noqa: BLE001 — job failure is status, not a crash
            detail, status = {"error": f"{type(exc).__name__}: {exc}"[:500]}, "failed"
        interval = float(wl["intervals_s"].get(name, 3600))
        st["jobs"][name] = {"last_started": started, "last_finished": _now(), "status": status, "detail": detail,
                            "interval_s": interval, "next_due_ts": now + interval,
                            "next_due": datetime.fromtimestamp(now + interval, UTC).isoformat(timespec="seconds")}
    _save_status(settings, st)
    return scheduler_status(settings)


def run_forever(settings: Settings, writer: Writer, tick_s: float = 30.0) -> None:  # pragma: no cover
    while True:
        run_due(settings, writer)
        time.sleep(tick_s)


def start_background(settings: Settings, writer: Writer, tick_s: float = 30.0):
    """Run the scheduler inside the API process with the API's own Writer (reviewer #11): call from the app
    lifespan, `stop = start_background(s, st.writer)`, and `stop()` on shutdown."""
    import threading

    stop_ev = threading.Event()

    def _loop() -> None:
        while not stop_ev.is_set():
            try:
                run_due(settings, writer)
            except Exception:  # noqa: BLE001 — the loop must survive any job failure
                pass
            stop_ev.wait(tick_s)

    t = threading.Thread(target=_loop, name="hydra-scheduler", daemon=True)
    t.start()
    return stop_ev.set


def api_running(settings: Settings, timeout: float = 0.5) -> bool:
    """True when a HYDRA API answers on the configured loopback port (then the CLI must not open a writer)."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{settings.api_host}:{settings.api_port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False
