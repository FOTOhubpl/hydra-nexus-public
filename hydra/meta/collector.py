"""One Meta collection cycle. Scheduled by exactly one owner: the WATCH-RT engine (source `meta-adlibrary`); the
Integrations "Run", the scheduler job `adlibrary_pull` and "Skanuj teraz" all go through `WatchRT.poll_source`, i.e.
through this function, its lock, its budget (hydra.meta.transport) and its outbox.

Per cycle: enabled queries in rotation (state `rotation`), at most `max_pages_per_query` pages per query, until HYDRA's
own budget is used up (then: deferred, the rotation resumes at the same query next time). Per query and mode a cursor
{after, page, date_min, watermark}; the Graph `paging.next` URL is never followed (it embeds the token): only the
`after` cursor, validated (string, bounded, not repeating). A pass ends on an empty page, on a page without a cursor,
or at PASS_MAX_PAGES (reported as `page_limit`); the next pass starts at `watermark - overlap_days` (late-indexed
ads) -- the date window is a policy, not a claim of a complete incremental stream.

Each page is stored with its cursor in one Writer transaction (`store.commit_page`): the cursor moves only after the
observations are durable; a crash before COMMIT re-reads the page and the UNIQUE keys make that idempotent.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from hydra.db import connect
from hydra.meta import contract, normalize, store
from hydra.meta.transport import CONFIG_STATES, MetaError, MetaTransport, scrub

PASS_MAX_PAGES = 20
CURSOR_MAX = 2000
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

STATUS_PL = {
    "ok": "Obieg zakończony.",
    "partial": "Obieg częściowy: część stron lub zapytań nie została pobrana (szczegóły w historii).",
    "empty": "Brak nowych wyników dla zapytań w tym obiegu.",
    "idle": "Brak aktywnych zapytań: dodaj hasła lub strony w konfiguracji.",
    "deferred_budget": "Wstrzymano do wznowienia: wyczerpany własny budżet HYDRY (to nie oznacza braku reklam).",
    "rate_limited": "Meta ograniczyła zapytania: praca odroczona.",
    "needs_config": "Wymagana konfiguracja (token).",
    "offline": "Tryb offline: brak połączeń.",
    "busy": "Inny obieg Meta właśnie trwa (jeden aktywny kolektor).",
    "error": "Błąd obiegu.",
}


@dataclass
class CycleResult:
    mode: str
    status: str
    message_pl: str
    requests: int = 0
    pages: int = 0
    records: int = 0
    new_ads: int = 0
    new_revisions: int = 0
    outbox_ids: list[int] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)
    normalized: list[dict[str, Any]] = field(default_factory=list)
    error_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "normalized"}
        d["ads_seen"] = len(self.normalized)
        return d


def _lock(settings: Any) -> threading.Lock:
    key = str(Path(settings.state_dir).resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class _ProcessLock:
    """One active Meta collector per state dir across processes (API, `hydra watch run`)."""

    def __init__(self, settings: Any) -> None:
        self.path = Path(settings.state_dir) / "meta" / "collector.lock"
        self.fh: Any = None

    def __enter__(self) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")  # noqa: SIM115
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def __exit__(self, *a: Any) -> None:
        if self.fh is not None:
            with contextlib.suppress(OSError):
                self.fh.close()


def _reader(settings: Any):
    return closing(connect(settings.db_path, readonly=True))


def build_params(cfg: dict[str, Any], q: dict[str, Any], date_min: str | None, fields: list[str],
                 after: str | None) -> dict[str, str]:
    p: dict[str, str] = {
        "ad_reached_countries": json.dumps(cfg["countries"]),
        "ad_active_status": cfg.get("ad_active_status") or "ALL",
        "ad_type": contract.AD_TYPE,
        "fields": ",".join(fields),
        "limit": str(int(cfg.get("page_size") or 50)),
    }
    if q["kind"] == "keyword":
        p["search_terms"] = q["value"][: contract.SEARCH_TERMS_MAX]
        p["search_type"] = "KEYWORD_EXACT_PHRASE" if len(q["value"].split()) > 1 else "KEYWORD_UNORDERED"
    else:
        p["search_page_ids"] = ",".join(q["value"].split(",")[: contract.PAGE_IDS_MAX])
    floor = cfg.get("date_from")  # rule "data wystąpienia od": a floor under the rolling window / watermark
    if floor and (not date_min or floor > date_min):
        date_min = floor
    if date_min:
        p["ad_delivery_date_min"] = date_min
    if cfg.get("date_to"):
        p["ad_delivery_date_max"] = cfg["date_to"]
    if cfg.get("platforms"):
        p["publisher_platforms"] = json.dumps(cfg["platforms"])
    if cfg.get("languages"):
        p["languages"] = json.dumps(cfg["languages"])
    if after:
        p["after"] = after
    return p


def fields_for(diag: dict[str, Any] | None) -> list[str]:
    """Required fields always; optional ones only when the last diagnostics saw them accepted."""
    ok = [f for f, v in ((diag or {}).get("optional_fields") or {}).items() if v == "available"]
    return [*contract.REQUIRED_FIELDS, *[f for f in contract.OPTIONAL_FIELDS if f in ok]]


def _valid_after(after: Any, prev: str | None) -> str | None:
    if not isinstance(after, str) or not after or len(after) > CURSOR_MAX or after == prev:
        return None
    return after


def _start_date(cur: dict[str, Any], cfg: dict[str, Any], today: date) -> str:
    mark = cur.get("watermark")
    if mark:
        with contextlib.suppress(ValueError):
            return (date.fromisoformat(mark) - timedelta(days=int(cfg.get("overlap_days") or 0))).isoformat()
    return (today - timedelta(days=int(cfg.get("date_window_days") or 7))).isoformat()


def run_cycle(settings: Any, writer: Any, *, fetch: Callable | None = None, trigger: str = "schedule",
              clock: Callable[[], float] = time.time, today: date | None = None,
              recorder: Any = None, only_query: str | None = None) -> CycleResult:
    """One live collection cycle (never raises for source failures)."""
    lk = _lock(settings)
    if not lk.acquire(blocking=False):
        return CycleResult("live", "busy", STATUS_PL["busy"])
    try:
        with _ProcessLock(settings) as got:
            if not got:
                return CycleResult("live", "busy", STATUS_PL["busy"])
            started = store.iso(clock())
            res = _cycle(settings, writer, fetch=fetch, trigger=trigger, clock=clock, today=today,
                         recorder=recorder, only_query=only_query)
            _record_cycle(writer, res, trigger, started, clock())
            return res
    finally:
        lk.release()


def _cycle(settings: Any, writer: Any, *, fetch: Callable | None, trigger: str, clock: Callable[[], float],
           today: date | None, recorder: Any, only_query: str | None) -> CycleResult:
    writer.run(store.ensure_schema)
    with _reader(settings) as conn:
        cfg = store.config(conn)
        diag = store.get_state(conn, "diagnostics", None)
        rr = int((store.get_state(conn, "rotation", {}) or {}).get("index") or 0)
    live_q = [q for q in cfg["queries"] if q["enabled"] and q["origin"] != "replay"]
    exp = cfg.get("expansion") or {}
    queries = [q for q in live_q if q["origin"] != "expansion"][: int(cfg.get("max_active_queries") or 10)]
    if exp.get("enabled"):  # P1 expansion queries: their own small quota inside the same budget
        queries += [q for q in live_q if q["origin"] == "expansion"][: int(exp.get("max_pages") or 0)]
    if only_query:
        queries = [q for q in live_q if q["id"] == only_query]
    res = CycleResult("live", "ok", STATUS_PL["ok"])
    if not queries:
        res.status, res.message_pl = "idle", STATUS_PL["idle"]
        return res
    tr = MetaTransport(settings, writer, fetch=fetch, budget_per_h=int(cfg["budget_per_h"]), clock=clock)
    try:
        tr.preflight()
    except MetaError as e:
        res.status = "needs_config" if e.status in CONFIG_STATES else (
            "deferred_budget" if e.status == "budget" else e.status)
        res.message_pl, res.error_status = e.message_pl, e.status
        _record_skip(writer, res, trigger, clock())
        return res
    fields = fields_for(diag)
    today = today or datetime.fromtimestamp(clock(), UTC).date()
    n = len(queries)
    statuses: list[str] = []
    stop = False
    done_idx = rr
    for i in range(n):
        if stop:
            break
        qi = (rr + i) % n
        q = queries[qi]
        cur = dict((q["cursor"] or {}).get("live") or {})
        date_min = cur.get("date_min") or _start_date(cur, cfg, today)
        after, page0 = cur.get("after"), int(cur.get("page") or 0)
        poll_id = writer.run(lambda c, q=q, cur=cur: store.start_poll(c, "live", q["id"], trigger, clock(), cur))  # noqa: B023
        qres = {"query_id": q["id"], "label": q["label"] or q["value"], "pages": 0, "records": 0, "status": "ok"}
        req0 = tr.requests
        usage = None
        pstatus, reason = "ok", None
        for k in range(int(cfg["max_pages_per_query"])):
            page = page0 + k
            params = build_params(cfg, q, date_min, fields, after)
            try:
                resp = tr.get(params)
            except MetaError as e:
                reason = e.message_pl
                if e.status in ("budget", "rate_limited", "offline") or e.status in CONFIG_STATES:
                    pstatus = "deferred_budget" if e.status == "budget" else (
                        "needs_config" if e.status in CONFIG_STATES else e.status)
                    stop = True
                    res.error_status = e.status
                else:  # params / transient / blocked / error: this query only; cursor kept for the next cycle
                    pstatus = "partial" if e.status in ("transient", "error") else "error"
                break
            usage = resp.usage or usage
            data = resp.data
            raw_rows = data.get("data") if isinstance(data.get("data"), list) else []
            rows = [r for r in raw_rows if isinstance(r, dict) and str(r.get("id") or "").strip()]
            dropped = len(raw_rows) - len(rows)
            ts = store.iso(clock())
            norm = [normalize.normalise(r, observed_at=ts, countries=cfg["countries"],
                                        graph_version=contract.graph_version(),
                                        requested_optional=[f for f in fields if f in contract.OPTIONAL_FIELDS])
                    for r in rows]
            nxt = _valid_after(((data.get("paging") or {}).get("cursors") or {}).get("after"), after)
            has_next = bool((data.get("paging") or {}).get("next")) and nxt is not None
            loop = ((data.get("paging") or {}).get("cursors") or {}).get("after") == after and after is not None
            if not rows or not has_next or page + 1 >= PASS_MAX_PAGES:
                new_cur = {"watermark": today.isoformat(), "last_pass_end": ts}
                pass_status = "page_limit" if (rows and has_next) else ("loop" if loop else "complete")
            else:
                new_cur = {"after": nxt, "page": page + 1, "date_min": date_min,
                           "watermark": cur.get("watermark")}
                pass_status = "continue"
            if recorder is not None:
                recorder.add(q, page, data)
            out = writer.run(_commit_fn(poll_id, q["id"], int(cfg["config_version"]), page, norm, clock,
                                        (q["id"], new_cur, pass_status)))
            res.pages += 1
            res.records += out["records"]
            res.new_ads += out["new_ads"]
            res.new_revisions += out["new_revisions"]
            res.outbox_ids += out["outbox_ids"]
            res.normalized += norm
            if exp.get("enabled") and out["new_ads"]:
                writer.run(_expand_fn(norm, q, exp, clock))
            qres["pages"] += 1
            qres["records"] += out["records"]
            if dropped:
                pstatus, reason = "partial", f"Pominięto {dropped} rekordów bez identyfikatora (odpowiedź częściowa)."
            if loop:
                pstatus, reason = "partial", "Zapętlony kursor stronicowania: przebieg zakończony."
            if pass_status != "continue":
                if pass_status == "page_limit":
                    reason = (reason or "") + f" Osiągnięto limit {PASS_MAX_PAGES} stron przebiegu."
                break
            after = nxt
        qres["status"] = pstatus
        if reason:
            qres["reason_pl"] = reason.strip()
        res.queries.append(qres)
        statuses.append(pstatus)
        reqs = tr.requests - req0
        final = pstatus if qres["records"] or pstatus != "ok" else "empty"
        writer.run(_finish_fn(poll_id, q["id"], final, pstatus, reason, reqs, usage, clock))
        if not stop:
            done_idx = qi + 1
    res.requests = tr.requests
    writer.run(lambda c: store.set_state(c, "rotation", {"index": done_idx % n if n else 0}, clock()))
    if recorder is not None:
        with contextlib.suppress(OSError):
            recorder.flush(clock())
    worst = [s for s in statuses if s != "ok"]
    if res.error_status:
        res.status = statuses[-1] if statuses else res.error_status
    elif worst:
        res.status = "partial"
    elif not res.records:
        res.status = "empty"
    res.message_pl = STATUS_PL.get(res.status, res.status)
    if res.status in ("deferred_budget", "rate_limited", "needs_config") and res.queries:
        res.message_pl = res.queries[-1].get("reason_pl") or res.message_pl
    return res


def _commit_fn(poll_id: int, qid: str, cfg_version: int, page: int, norm: list[dict[str, Any]],
               clock: Callable[[], float], qid_cursor: tuple[str, dict[str, Any] | None, str]) -> Callable:
    def tx(c: Any) -> dict[str, Any]:
        return store.commit_page(c, poll_id=poll_id, mode="live", query_id_=qid, config_version=cfg_version, page=page,
                                 normalized=norm, now=clock(), qid_cursor=qid_cursor)

    return tx


def _finish_fn(poll_id: int, qid: str, final: str, pstatus: str, reason: str | None, reqs: int, usage: Any,
               clock: Callable[[], float]) -> Callable:
    def tx(c: Any) -> None:
        store.finish_poll(c, poll_id, clock(), status=final, reason=reason, requests=reqs, usage=usage)
        if pstatus != "ok":  # keep the cursor (resume), record the status on the query
            store.set_query_cursor(c, qid, "live", _cursor_of(c, qid), clock(), status=pstatus, error=reason)

    return tx


def _expand_fn(norm: list[dict[str, Any]], parent: dict[str, Any], exp: dict[str, Any],
               clock: Callable[[], float]) -> Callable:
    """P1: the page of a newly seen ad becomes a `page_ids` query (origin `expansion`, depth + 1, parent ad), within
    `max_depth` and `max_pages` expansion queries. Provenance is explicit; the link is a hypothesis to verify."""
    depth = int(parent.get("depth") or 0) + 1

    def tx(c: Any) -> int:
        if depth > int(exp.get("max_depth") or 0):
            return 0
        have = c.execute("SELECT COUNT(*) FROM meta_queries WHERE origin='expansion'").fetchone()[0]
        added = 0
        for n in norm:
            pid = n["creative"].get("source_page_id")
            if not pid or not str(pid).isdigit() or have + added >= int(exp.get("max_pages") or 0):
                continue
            exists = c.execute("SELECT 1 FROM meta_queries WHERE kind='page_ids' AND value=?", (str(pid),)).fetchone()
            if exists:
                continue
            store.add_query(c, "page_ids", str(pid), clock(), origin="expansion", depth=depth,
                            parent_ad_id=n["creative"]["source_ad_id"],
                            label=f"Rozszerzenie: strona {pid} (z reklamy {n['creative']['source_ad_id']})")
            added += 1
        return added

    return tx


def _record_cycle(writer: Any, res: CycleResult, trigger: str, started: str, now: float) -> None:
    """BONUS-AUTO: one summary row per cycle (new / changed / unchanged / ended ads), hydra.meta.auto."""
    from hydra.meta import auto

    row = auto.cycle_row(res, trigger=trigger, started=started, finished=store.iso(now))
    with contextlib.suppress(Exception):
        writer.run(lambda c: auto.record_cycle(c, row, now))


def _cursor_of(conn: Any, qid: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT cursor FROM meta_queries WHERE id=?", (qid,)).fetchone()
    return (json.loads(r[0] or "{}") or {}).get("live") if r else None


def _record_skip(writer: Any, res: CycleResult, trigger: str, now: float) -> None:
    """A cycle that made no request (no token, offline, budget, provider back-off) is still visible in the history."""
    def tx(c: Any) -> None:
        pid = store.start_poll(c, "live", None, trigger, now)
        store.finish_poll(c, pid, now, status=res.status, reason=res.message_pl)

    with contextlib.suppress(Exception):
        writer.run(tx)


# ------------------------------------------------------------------------------------------------ replay
PAGES_FORMAT = "hydra.meta.pages.v1"


def legacy_rows(metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The WATCH-RT demo fixture's flat list -> Graph-shaped rows (synthetic; `hydra_landing_urls` marks fixture
    links, never produced from a Graph answer)."""
    out = []
    for m in metas:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        out.append({"id": str(m["id"]), "page_id": m.get("page_id"), "page_name": m.get("page_name"),
                    "ad_creative_bodies": [m["text"]] if m.get("text") else [],
                    "ad_delivery_start_time": m.get("ad_delivery_start_time"),
                    "hydra_landing_urls": list(m.get("link_urls") or []),
                    "hydra_keyword": m.get("keyword")})
    return out


def replay_body(settings: Any, writer: Any, body: bytes, *, now: float, trigger: str = "replay") -> CycleResult:
    """A replay tick (fixture or recording) through the same normalisation, store and outbox, in the `synthetic` or
    `recorded` data space. No network."""
    writer.run(store.ensure_schema)
    try:
        doc = json.loads(body or b"[]")
    except ValueError:
        return CycleResult("synthetic", "error", "Niepoprawny JSON w nagraniu Meta.")
    if isinstance(doc, list):
        mode, pages = "synthetic", [{"query": {"kind": "keyword", "value": "demo"}, "page": 0,
                                     "data": {"data": legacy_rows(doc)}}]
    elif isinstance(doc, dict) and doc.get("format") == PAGES_FORMAT:
        mode = "synthetic" if doc.get("synthetic", True) else "recorded"
        pages = [p for p in doc.get("pages") or [] if isinstance(p, dict)]
    else:
        return CycleResult("synthetic", "error", "Nieznany format nagrania Meta.")
    res = CycleResult(mode, "ok", "Odtworzenie: " + store.MODE_PL[mode])
    from hydra.meta import auto

    with _reader(settings) as conn:
        cfg = store.config(conn)
        probe = auto.campaign_probe(conn) if mode == "synthetic" else None
    if probe and probe.get("image"):  # the member's ad image (local CAS), as the probe's synthetic fixture media
        import base64

        from hydra.storage import ContentStore

        with contextlib.suppress(Exception):
            probe["image_b64"] = base64.b64encode(ContentStore(settings.cas_dir).read_bytes(probe["image"])).decode()
    for p in pages:
        q = p.get("query") or {}
        rows = [r for r in ((p.get("data") or {}).get("data") or []) if isinstance(r, dict) and r.get("id")]
        # synthetic campaign probe rows: filled from an existing local campaign, dropped when there is none
        rows = [auto.probe_row(r, probe) if r.get("hydra_campaign_probe") else r for r in rows
                if not r.get("hydra_campaign_probe") or probe]
        ts = store.iso(now)
        norm = []
        for r in rows:
            n = normalize.normalise(r, observed_at=ts, countries=cfg["countries"],
                                    graph_version=(p.get("graph_api_version") or doc.get("graph_api_version")
                                                   if isinstance(doc, dict) else None))
            for u in r.get("hydra_landing_urls") or []:
                n["creative"]["landing_urls"].append({"url": str(u), "provenance": "fixture_link",
                                                      "field": "hydra_landing_urls"})
            if r.get("hydra_synthetic_media"):
                n["meta"]["synthetic_media"] = r["hydra_synthetic_media"][:4]
            norm.append(n)

        def tx(c: Any, q: dict[str, Any] = q, norm: list = norm, page: int = int(p.get("page") or 0)) -> dict:
            qid = None
            with contextlib.suppress(ValueError):
                # own query rows (never the live query of the same text): replay provenance stays separate
                qid = store.add_query(c, "keyword", f"[{mode}] {q.get('value') or 'demo'}"[:100], now,
                                      origin="replay", label=f"{q.get('value') or 'demo'} ({store.MODE_PL[mode]})",
                                      enabled=False)
            pid = store.start_poll(c, mode, qid, trigger, now)
            out = store.commit_page(c, poll_id=pid, mode=mode, query_id_=qid, config_version=0, page=page,
                                    normalized=norm, now=now)
            store.finish_poll(c, pid, now, status="ok" if norm else "empty")
            return out

        out = writer.run(tx)
        res.pages += 1
        res.records += out["records"]
        res.new_ads += out["new_ads"]
        res.new_revisions += out["new_revisions"]
        res.outbox_ids += out["outbox_ids"]
        res.normalized += norm
    if not res.records:
        res.status = "empty"
    _record_cycle(writer, res, trigger, store.iso(now), now)
    return res


class PageRecorder:
    """HYDRA_WATCH_RECORD=1 (live only): token-free page recording as a WATCH-RT fixture tick
    (`<state>/watch/rt/recordings/meta-live-<date>/`, manifest `synthetic: false`, replayed as ODTWORZENIE). The
    token, `access_token=` parameters and the `paging.next` URLs are removed before anything is written."""

    def __init__(self, settings: Any, token: str | None) -> None:
        self.root = Path(settings.state_dir) / "watch" / "rt" / "recordings"
        self.token = token
        self.pages: list[dict[str, Any]] = []

    def add(self, q: dict[str, Any], page: int, data: dict[str, Any]) -> None:
        text = json.dumps(scrub(json.loads(json.dumps(data)), self.token), ensure_ascii=False)
        d = json.loads(re.sub(r"[?&]access_token=[^&\"\\]*", "", text))
        if isinstance(d.get("paging"), dict):
            d["paging"].pop("next", None)
            d["paging"].pop("previous", None)
        self.pages.append({"query": {"kind": q["kind"], "value": q["value"]}, "page": page, "data": d,
                           "graph_api_version": contract.graph_version()})

    def flush(self, now: float) -> Path | None:
        if not self.pages:
            return None
        day = store.iso(now)[:10]
        d = self.root / f"meta-live-{day}"
        d.mkdir(parents=True, exist_ok=True)
        mp = d / "manifest.json"
        man = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {
            "name": d.name, "synthetic": False, "interval_s": 60, "ticks": [],
            "description_pl": f"Nagranie odpowiedzi Meta Ad Library z {day} (bez tokenu; HYDRA_WATCH_RECORD=1)."}
        name = f"t{len(man['ticks']):05d}-meta-adlibrary.json"
        body = {"format": PAGES_FORMAT, "synthetic": False, "recorded_at": store.iso(now), "pages": self.pages}
        text = json.dumps(body, ensure_ascii=False)
        if self.token and self.token in text:  # defence in depth: never write a token
            raise OSError("token in recording")
        (d / name).write_text(text, encoding="utf-8")
        man["ticks"].append({"meta-adlibrary": name})
        mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
        self.pages = []
        return d / name


def recorder_for(settings: Any) -> PageRecorder | None:
    if os.environ.get("HYDRA_WATCH_RECORD", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    from hydra.integrations.secrets import meta_token

    return PageRecorder(settings, meta_token())
