"""WATCH-RT engine: an in-process supervised scheduler for public feeds.

One asyncio task (started with the API lifespan when HYDRA_WATCH_RT=1, by `POST /v1/watch/rt/engine {start}`, or by
`hydra watch run`) wakes at least once a second, polls every due source in a worker thread and sleeps until the next
due time. Per source: interval (default 60 s, never below the provider policy / registry rate limit), +0-10 % jitter,
ETag / If-Modified-Since validators, exponential backoff on errors (interval x 2^n, max 1 h), a circuit breaker
(5 consecutive errors -> open for 15 min, then one half-open trial), health columns in `watch_sources`.

Network rules (fail closed, no fallback): a live poll happens only when HYDRA_OFFLINE=0 AND the engine runs AND the
source is enabled; every request goes through `hydra.integrations.http.request` (connector enabled flag, netguard,
rate limit, audit row). Offline -> engine state `offline` with a Polish message, nothing is attempted. Replay mode
feeds fixture snapshots through the same adapters, store and matcher; every replay row carries replay=1 and the UI
labels it SYNTETYCZNE / ODTWORZENIE.

Each new item: normalised indicators -> exact lookup in the watchlist index -> `watch_matches` rows in the same
transaction as the item. Optional (HYDRA_WATCH_AUTO_ASSIGN=1, live only): a strong URL/host match is sent through
the public `hydra.batch.assign_batch` (assign / new_campaign / abstain + new_pattern_id); scoring is never touched.
Events for the live stream (watch.poll, watch.item, watch.match, watch.source_state) go to an in-memory ring that
`hydra.api_stream.Hub` forwards to `GET /v1/stream`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hydra import netguard
from hydra.db import connect
from hydra.watch.rt import store
from hydra.watch.rt.sources import ADAPTERS, PollCtx, Resp, SourceError, effective_min_interval
from hydra.watch.rt.watchlist import Index, derive

log = logging.getLogger("hydra.watch.rt")

JITTER = 0.10
MAX_BACKOFF_S = 3600
CB_THRESHOLD = 5
CB_OPEN_S = 900
TICK_MAX_S = 1.0
RING = 5000
HOUSEKEEP_S = 3600
FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPLAY_LABEL_PL = "SYNTETYCZNE / ODTWORZENIE — dane z nagrania, nie z sieci na żywo"

STATE_PL = {
    "stopped": "Wyłączony", "live": "Na żywo", "offline": "Offline", "replay": "Odtworzenie", "paused": "Wstrzymany",
}
SOURCE_STATE_PL = {
    "ok": "Działa", "idle": "Oczekuje", "disabled": "Wyłączone", "offline": "Offline", "backoff": "Ponawianie",
    "circuit_open": "Wstrzymane po błędach", "needs_key": "Brak klucza", "error": "Błąd", "replay": "Odtworzenie",
    "paused": "Wstrzymane", "no_new": "Brak nowych wpisów",
}
MSG_OFFLINE_PL = ("Tryb offline (HYDRA_OFFLINE=1): Watch nie odpytuje żadnych źródeł i nie korzysta z danych "
                  "zastępczych. Aby uruchomić podgląd na żywo, uruchom HYDRA z HYDRA_OFFLINE=0 i włącz źródła. "
                  "Tryb demonstracyjny (odtworzenie) działa offline.")
MSG_STOPPED_PL = "Silnik Watch jest wyłączony. Kliknij Uruchom (lub HYDRA_WATCH_RT=1 przy starcie API)."


def local_pl(ts: str | None, seconds: bool = False) -> str | None:
    """UTC ISO -> Europe/Warsaw wall time "HH:MM" (or with seconds); "DD.MM HH:MM" when not today."""
    if not ts:
        return None
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    try:
        d = _dt.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        from datetime import UTC as _UTC

        d = d.replace(tzinfo=_UTC)
    tz = ZoneInfo("Europe/Warsaw")
    d = d.astimezone(tz)
    t = d.strftime("%H:%M:%S" if seconds else "%H:%M")
    return t if d.date() == _dt.now(tz).date() else f"{d.strftime('%d.%m')} {t}"


def size_pl(n: int | None) -> str | None:
    if not n:
        return None
    from hydra.labels_pl import num_pl

    return f"{num_pl(n / 1e6)} MB" if n >= 100_000 else f"{num_pl(n / 1e3)} kB"


def _every_pl(interval: int) -> str:
    return "co minutę" if interval <= 60 else f"co {interval // 60} min" if interval < 3600 else \
        f"co {interval // 3600} h"


def _entries_pl(n: int) -> str:
    if n == 1:
        return "1 nowy wpis"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} nowe wpisy"
    return f"{n} nowych wpisów"


def summary_pl(row: dict[str, Any], f: dict[str, Any]) -> str | None:
    """One sentence for the source card, e.g. "Lista CERT bez nowych wpisów od 02:47 (ostatni wpis 03:01:55 czasu
    PL). Plik sprawdzany co minutę." Only facts from the rows; None before the first successful poll."""
    if not row.get("last_ok"):
        return None
    sid = row["id"]
    every = _every_pl(int(row.get("interval_s") or 60))
    last_new, newest = local_pl(f.get("last_new_at")), local_pl(f.get("newest_entry"), seconds=True)
    newest_txt = f" (ostatni wpis {newest} czasu PL)" if newest else ""
    if sid == "cert-pl":
        what, checked = "Lista CERT", f"Plik sprawdzany {every}."
    else:
        what, checked = ADAPTERS[sid].name_pl.split(" — ")[0], f"Źródło sprawdzane {every}."
    if row.get("state") == "no_new" or not f.get("new_24h"):
        since = f" od {last_new}" if last_new else ""
        return f"{what} bez nowych wpisów{since}{newest_txt}. {checked}"
    return (f"{what}: {_entries_pl(int(f['new_24h']))} w ostatnich 24 h, ostatnie nowe o {last_new}{newest_txt}. "
            f"{checked}")


def result_pl(sid: str, run: dict[str, Any], cursor: dict[str, Any]) -> str:
    """"Odpytaj teraz" answer, e.g. "Sprawdzono: brak nowych wpisów (plik bez zmian, 31,6 MB)."."""
    size = size_pl(cursor.get("file_size")) if sid == "cert-pl" else None
    extra = f"; {run['error_detail']}" if run.get("error_detail") else ""
    if not run.get("new"):
        if sid == "cert-pl":
            inner = "plik bez zmian" if run.get("not_modified") else "brak nowych blokad w przyroście pliku"
            return f"Sprawdzono: brak nowych wpisów ({inner}{', ' + size if size else ''}{extra})."
        inner = "bez zmian od ostatniego pobrania" if run.get("not_modified") else "wszystkie pozycje już znane"
        return f"Sprawdzono: brak nowych wpisów ({inner}{extra})."
    m = f", dopasowania: {run['matched']}" if run.get("matched") else ""
    return f"Sprawdzono: {_entries_pl(int(run['new']))}{m}{' (plik ' + size + ')' if size else ''}{extra}."


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------------------------------------ clocks
class Clock:
    def time(self) -> float:
        return time.time()

    async def sleep(self, s: float, wake: asyncio.Event | None = None) -> None:
        if wake is None:
            await asyncio.sleep(s)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=max(0.0, s))


class FakeClock(Clock):
    """Deterministic test clock: sleep advances time instantly."""

    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s

    async def sleep(self, s: float, wake: asyncio.Event | None = None) -> None:
        self.t += max(0.0, s)
        await asyncio.sleep(0)


# ------------------------------------------------------------------------------------------------ fetchers
def live_fetcher(settings: Any, writer: Any) -> Callable[[str, str, str, dict[str, str]], Resp]:
    from hydra.integrations import http, secrets

    def fetch(connector_id: str, method: str, url: str, headers: dict[str, str]) -> Resp:
        sec = [secrets.get_secret(connector_id, n) for n in ("URLHAUS_AUTH_KEY",) if connector_id == "urlhaus"]
        if connector_id == "meta-adlibrary":
            sec.append(secrets.meta_token())  # scrubbed from every message (the token is a query parameter)
        try:
            r = http.request(connector_id, method, url, headers=headers, settings=settings, writer=writer,
                             secrets=sec, timeout=60.0 if connector_id == "crtsh" else 30.0,
                             max_bytes=20_000_000)
        except http.IntegrationError as e:
            raise SourceError(e.status, e.message_pl, e.http_status) from None
        return Resp(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.body, r.ms)

    return fetch


@dataclass
class Fixture:
    name: str
    path: Path
    manifest: dict[str, Any]

    @property
    def ticks(self) -> list[dict[str, str]]:
        return list(self.manifest.get("ticks") or [])

    @property
    def synthetic(self) -> bool:
        return bool(self.manifest.get("synthetic", True))


def list_fixtures(extra: Path | None = None) -> list[Fixture]:
    out = []
    roots = [FIXTURES_DIR] + ([extra] if extra else [])
    rec = os.environ.get("HYDRA_WATCH_REC_ROOT")
    if rec:
        roots.append(Path(rec).expanduser())
    env = os.environ.get("HYDRA_WATCH_FIXTURES")
    if env:
        roots.append(Path(env).expanduser())
    if extra is None:
        with contextlib.suppress(Exception):
            from hydra.config import get_settings

            st = get_settings().state_dir
            if st is not None:
                roots.append(Path(st) / "watch" / "rt" / "recordings")
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            m = d / "manifest.json"
            if m.is_file():
                with contextlib.suppress(OSError, ValueError):
                    out.append(Fixture(d.name, d, json.loads(m.read_text(encoding="utf-8"))))
    return out


def load_fixture(name_or_path: str) -> Fixture:
    p = Path(name_or_path).expanduser()
    if p.is_dir() and (p / "manifest.json").is_file():
        return Fixture(p.name, p, json.loads((p / "manifest.json").read_text(encoding="utf-8")))
    for f in list_fixtures():
        if f.name == name_or_path:
            return f
    raise FileNotFoundError(name_or_path)


class Recorder:
    """HYDRA_WATCH_RECORD=1 (live only): every successful GET body of a poll is saved as one fixture tick under
    `<state>/watch/rt/recordings/<UTC date>/` (manifest.json `synthetic: false`), replayable with the same engine
    (`hydra watch replay <dir>`; UI label ODTWORZENIE). Public feed responses only; stays in the state dir."""

    EXT = {"cert-pl": "log", "urlhaus": "json", "openphish": "txt", "crtsh": "json", "meta-adlibrary": "json"}

    def __init__(self, root: Path) -> None:
        self.root = root
        self._bodies: list[bytes] = []

    def wrap(self, fetch: Callable[..., Resp]) -> Callable[..., Resp]:
        self._bodies = []

        def rec(connector_id: str, method: str, url: str, headers: dict[str, str]) -> Resp:
            r = fetch(connector_id, method, url, headers)
            if connector_id == "meta-adlibrary":
                return r  # raw Graph pages carry the token in paging.next: META-COLLECT records scrubbed pages itself
            if method == "GET" and r.status in (200, 206) and r.body:
                self._bodies.append(r.body)
            return r

        return rec

    def flush(self, sid: str, now: float) -> None:
        if not self._bodies:
            return
        body = self._bodies[0]
        if sid == "crtsh" and len(self._bodies) > 1:  # several keyword queries -> one JSON list
            merged: list[Any] = []
            for b in self._bodies:
                with contextlib.suppress(ValueError):
                    merged += json.loads(b) or []
            body = json.dumps(merged).encode()
        self._bodies = []
        day = store.iso(now)[:10]
        d = self.root / f"live-{day}"
        d.mkdir(parents=True, exist_ok=True)
        mp = d / "manifest.json"
        man = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {
            "name": d.name, "synthetic": False, "interval_s": 60, "ticks": [],
            "description_pl": f"Nagranie odpowiedzi źródeł na żywo z {day} (HYDRA_WATCH_RECORD=1)."}
        n = len(man["ticks"])
        name = f"t{n:05d}-{sid}.{self.EXT.get(sid, 'bin')}"
        (d / name).write_bytes(body)
        man["ticks"].append({sid: name})
        mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")


class ReplayFetcher:
    """Serves one fixture tick: the file named for the source in the tick, else 304 (nothing new)."""

    def __init__(self, fixture: Fixture, extra: dict[str, list[str]] | None = None) -> None:
        self.fixture = fixture
        self.tick: dict[str, str] = {}
        self.tick_no = -1
        self.extra = extra or {}  # tick index -> extra CERT lines (campaign probes)

    def set_tick(self, i: int) -> None:
        self.tick_no = i
        self.tick = self.fixture.ticks[i] if 0 <= i < len(self.fixture.ticks) else {}

    def __call__(self, connector_id: str, method: str, url: str, headers: dict[str, str]) -> Resp:
        if method == "HEAD":
            return Resp(200, {"content-length": "0"}, b"")
        name = self.tick.get(connector_id)
        body = b""
        if name:
            p = (self.fixture.path / name).resolve()
            if p.parent != self.fixture.path.resolve() or not p.is_file():
                raise SourceError("parse", f"Nagranie: brak pliku {name}.")
            body = p.read_bytes()
        lines = self.extra.get(f"{self.tick_no}:{connector_id}")
        if lines:
            body = body + ("\n".join(lines) + "\n").encode()
        if not body:
            return Resp(304, {}, b"")
        return Resp(206 if connector_id == "cert-pl" else 200, {}, body)


# ------------------------------------------------------------------------------------------------ engine
@dataclass
class Replay:
    fixture: Fixture
    speed: float
    fetcher: ReplayFetcher
    ticks_done: int = 0
    done: bool = False
    started_at: str = ""
    probes: int = 0


@dataclass
class Metrics:
    loop_ms: deque = field(default_factory=lambda: deque(maxlen=2000))
    match_us: deque = field(default_factory=lambda: deque(maxlen=20000))


class WatchRT:
    def __init__(self, settings: Any, writer: Any, *, clock: Clock | None = None,
                 fetcher: Callable[..., Resp] | None = None, rng: random.Random | None = None,
                 auto_assign: bool | None = None) -> None:
        self.settings = settings
        self.writer = writer
        self.clock = clock or Clock()
        self.fetcher = fetcher or live_fetcher(settings, writer)
        self.rng = rng or random.Random()
        self.index = Index()
        self.state = "stopped"
        self.started_at: str | None = None
        self.replay: Replay | None = None
        self.metrics = Metrics()
        self._ring: deque[dict[str, Any]] = deque(maxlen=RING)
        self._seq = 0
        self._ring_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_housekeep = 0.0
        self.auto_assign = env_flag("HYDRA_WATCH_AUTO_ASSIGN") if auto_assign is None else auto_assign
        self._assign_q: deque[int] = deque(maxlen=200)
        self._lockfile: Any = None
        self.recorder = Recorder(Path(self.settings.state_dir) / "watch" / "rt" / "recordings") \
            if env_flag("HYDRA_WATCH_RECORD") else None
        self.writer.run(lambda c: (store.ensure_schema(c), store.ensure_sources(c, self.clock.time())))
        self.reload_index()
        self.meta_worker: Any = None  # META-COLLECT analysis worker (outbox), started with live or replay mode

    # ---------------------------------------------------------------- plumbing
    def reader(self):
        return closing(connect(self.settings.db_path, readonly=True))

    def reload_index(self) -> None:
        with self.reader() as conn:
            self.index.load(conn)

    def emit(self, type_: str, data: dict[str, Any]) -> None:
        with self._ring_lock:
            self._seq += 1
            self._ring.append({"seq": self._seq, "type": type_, "data": data})
        if self._loop is not None and self._wake is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._wake.set)

    def events_since(self, seq: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._ring_lock:
            if seq > self._seq:  # engine restarted: resend the retained tail
                seq = 0
            return [e for e in self._ring if e["seq"] > seq][:limit]

    @property
    def seq(self) -> int:
        return self._seq

    def live_allowed(self) -> bool:
        return not netguard.offline(self.settings)

    # ---------------------------------------------------------------- status
    def source_view(self, row: dict[str, Any], now: float) -> dict[str, Any]:
        from hydra.integrations import registry
        from hydra.integrations import state as istate

        a = ADAPTERS[row["id"]]
        c = registry.get(a.connector_id)
        lim = getattr(c, "rate_limit_per_min", None) if c else None
        state = row["state"]
        if self.state == "replay":
            state = "replay"
        elif row["enabled"] and self.state == "offline":
            state = "offline"
        elif row["enabled"] and self.state == "paused":
            state = "paused"
        nd = store.parse_iso(row["next_due"])
        due_in = None
        if row["enabled"] and nd is not None and self.state in ("live", "replay"):
            due_in = max(0, int(round(nd - now)))
        return {"id": a.id, "name_pl": a.name_pl, "description_pl": a.description_pl, "enabled": bool(row["enabled"]),
                "state": state, "state_pl": SOURCE_STATE_PL.get(state, state),
                "interval_s": max(int(row["interval_s"]), effective_min_interval(a)),
                "min_interval_s": effective_min_interval(a), "next_due": row["next_due"], "next_due_in_s": due_in,
                "last_poll": row["last_poll"], "last_ok": row["last_ok"], "last_latency_ms": row["last_latency_ms"],
                "last_error_pl": row["last_error"], "consecutive_errors": row["consecutive_errors"],
                "circuit_open_until": row["circuit_open_until"], "total_polls": row["total_polls"],
                "total_items": row["total_items"], "connector_id": a.connector_id, "sends_pl": a.sends_pl,
                "needs_secret": a.secret, "etag": bool(row["etag"] or row["last_modified"]),
                "rate_limit": {"limit_per_min": lim,
                               "used_60s": istate.requests_since(self.settings, a.connector_id, 60.0)},
                **self._freshness(row, now)}

    def _freshness(self, row: dict[str, Any], now: float) -> dict[str, Any]:
        """Source card facts: file size / Last-Modified / newest entry (CERT cursor), last new entry, 24 h count,
        hourly sparkline and one Polish sentence that says plainly whether the list simply has nothing new."""
        try:
            with self.reader() as conn:
                st = store.source_stats(conn, row["id"], now, replay=self.state == "replay")
        except Exception:  # noqa: BLE001 — a status view never fails on its decoration
            return {}
        cur = row.get("cursor") or {}
        newest = max(filter(None, [st["newest_entry"], cur.get("newest_entry")]), default=None)
        fresh = {"file_size": cur.get("file_size"), "server_last_modified": cur.get("server_last_modified"),
                 "newest_entry": newest, "last_new_at": st["last_new_at"], "new_24h": st["new_24h"],
                 "hourly_new": st["hourly"], "state_detail_pl": row.get("state_detail")}
        fresh["summary_pl"] = summary_pl(row, fresh)
        return fresh

    def status(self) -> dict[str, Any]:
        now = self.clock.time()
        with self.reader() as conn:
            rows = store.sources(conn)
            rep = self.state == "replay"  # live views never mix in replay rows, and vice versa
            out = {"sources": [self.source_view(rows[s], now) for s in ADAPTERS if s in rows],
                   "totals": store.totals(conn, now),
                   "sparkline": store.sparkline(conn, now, 60, replay=rep),
                   "timeline": store.timeline(conn, now, 48, replay=rep)}
        msg = None
        if self.state == "offline":
            msg = MSG_OFFLINE_PL
        elif self.state == "stopped":
            msg = MSG_STOPPED_PL if self.live_allowed() else MSG_OFFLINE_PL
        elif self.state == "replay":
            msg = REPLAY_LABEL_PL
        r = self.replay
        out["engine"] = {
            "state": self.state, "state_pl": STATE_PL[self.state], "message_pl": msg, "started_at": self.started_at,
            "now": store.iso(now), "offline": not self.live_allowed(), "auto_assign": self.auto_assign,
            "watchlist_index": self.index.size, "ct_keywords": len(self.index.keywords), "event_seq": self._seq,
            "replay": None if r is None else {
                "fixture": r.fixture.name, "speed": r.speed, "synthetic": r.fixture.synthetic,
                "label_pl": REPLAY_LABEL_PL, "description_pl": r.fixture.manifest.get("description_pl"),
                "ticks_total": len(r.fixture.ticks), "ticks_done": r.ticks_done, "done": r.done,
                "started_at": r.started_at, "campaign_probes": r.probes}}
        return out

    # ---------------------------------------------------------------- scheduling
    def due(self, now: float) -> list[str]:
        with self.reader() as conn:
            rows = store.sources(conn)
        out = []
        for sid, r in rows.items():
            if sid not in ADAPTERS or not r["enabled"]:
                continue
            nd = store.parse_iso(r["next_due"])
            if nd is None or nd <= now:
                out.append(sid)
        return sorted(out)

    def next_wake(self, now: float) -> float:
        with self.reader() as conn:
            rows = store.sources(conn)
        dues = [store.parse_iso(r["next_due"]) or now for r in rows.values() if r["enabled"] and r["id"] in ADAPTERS]
        return max(0.0, min([d - now for d in dues] + [TICK_MAX_S]))

    def _interval(self, sid: str, row: dict[str, Any]) -> int:
        return max(int(row["interval_s"]), effective_min_interval(ADAPTERS[sid]))

    def _jittered(self, s: float) -> float:
        return s * (1.0 + self.rng.uniform(0.0, JITTER))

    def backoff_s(self, interval: int, errors: int) -> float:
        return float(min(MAX_BACKOFF_S, interval * (2 ** max(0, errors))))

    def poll_source(self, sid: str, *, force: bool = False, replay: bool | None = None) -> dict[str, Any]:
        """One poll of one source (worker thread / CLI / tests). Never raises for source failures."""
        with self._poll_lock:
            return self._poll(sid, force=force, replay=self.state == "replay" if replay is None else replay)

    def _poll(self, sid: str, *, force: bool, replay: bool) -> dict[str, Any]:
        a = ADAPTERS[sid]
        now = self.clock.time()
        with self.reader() as conn:
            row = store.sources(conn)[sid]
        if replay and self.replay is None:
            raise SourceError("replay", "Brak aktywnego odtworzenia.")  # never fall through to the live fetcher
        if not replay and not self.live_allowed():
            raise SourceError("offline", MSG_OFFLINE_PL)
        if not replay and not force and not row["enabled"]:
            raise SourceError("disabled", "Źródło jest wyłączone.")
        interval = self._interval(sid, row)
        sec: dict[str, str | None] = {}
        if a.secret and not replay:
            from hydra.integrations import secrets

            sec[a.secret] = secrets.meta_token() if sid == "meta-adlibrary" else secrets.get_secret(
                a.connector_id, a.secret)
        fetch = self.replay.fetcher if (replay and self.replay) else self.fetcher
        recorder = self.recorder if not replay else None
        if recorder is not None:
            fetch = recorder.wrap(fetch)
        kws = list(self.index.keywords)
        if replay and self.replay:
            kws = sorted(set(kws) | set(self.replay.fixture.manifest.get("ct_keywords") or []))
        ctx = PollCtx(fetch=fetch, cursor=row["cursor"], etag=row["etag"], last_modified=row["last_modified"],
                      keywords=kws, secrets=sec, settings=self.settings, writer=self.writer, replay=replay,
                      now=store.iso(now), trigger="poll_now" if force else "schedule", clock=self.clock.time)
        t0 = time.perf_counter()
        err: SourceError | None = None
        out = None
        try:
            out = a.poll(ctx)
        except SourceError as e:
            err = e
        except Exception as e:  # noqa: BLE001 — an adapter bug is a source error, never an engine crash
            err = SourceError("error", f"{type(e).__name__}: {str(e)[:200]}")
        ms = int((time.perf_counter() - t0) * 1000)
        now2 = self.clock.time()
        fields: dict[str, Any] = {"last_poll": store.iso(now2), "last_latency_ms": ms,
                                  "total_polls": row["total_polls"] + 1}
        matches: dict[str, list] = {}
        items = []
        if err is None and out is not None:
            items = out.items
            for it in items:
                m0 = time.perf_counter_ns()
                hits = self.index.match(it.indicators)
                self.metrics.match_us.append((time.perf_counter_ns() - m0) / 1000)
                if hits:
                    matches[it.external_id] = hits
            fields.update(state="ok", state_detail=out.note_pl, last_ok=store.iso(now2), consecutive_errors=0,
                          circuit_open_until=None, last_error=None,
                          next_due=store.iso(now2 + self._jittered(interval)))
            if out.cursor is not None:
                fields["cursor"] = out.cursor
            if out.etag is not None or out.last_modified is not None:
                fields.update(etag=out.etag, last_modified=out.last_modified)
        else:
            assert err is not None
            fields["last_error"] = err.message_pl[:500]
            if err.status in ("needs_key", "disabled", "offline"):
                fields.update(state=err.status if err.status != "disabled" else "error",
                              next_due=store.iso(now2 + interval))
            elif err.status == "rate_limited":
                fields.update(state="backoff", next_due=store.iso(now2 + 60))
            else:
                n = row["consecutive_errors"] + 1
                fields["consecutive_errors"] = n
                if n >= CB_THRESHOLD:
                    until = now2 + CB_OPEN_S
                    fields.update(state="circuit_open", circuit_open_until=store.iso(until), next_due=store.iso(until))
                else:
                    wait = min(float(MAX_BACKOFF_S), self._jittered(self.backoff_s(interval, n)))  # cap after jitter
                    fields.update(state="backoff", next_due=store.iso(now2 + wait))
        if recorder is not None and err is None:
            with contextlib.suppress(OSError):
                recorder.flush(sid, now2)
        res = self.writer.run(lambda c: store.commit_poll(
            c, sid, started=now, duration_ms=ms, items=items, matches=matches,
            http_status=(out.http_status if out else err.http_status if err else None),
            not_modified=bool(out and out.not_modified), error=fields.get("last_error"), replay=replay,
            source_fields=dict(fields), now=now2, detail=out.detail_pl if out else None))
        run = {"run_id": res["run_id"], "source": sid, "fetched": len(items), "new": len(res["item_ids"]),
               "matched": len(res["match_ids"]), "duration_ms": ms, "error_pl": fields.get("last_error"),
               "error_status": err.status if err else None, "replay": replay,
               "not_modified": bool(out and out.not_modified), "error_detail": out.detail_pl if out else None}
        if err is None and out is not None:
            run["result_pl"] = result_pl(sid, run, out.cursor or {})
        self._emit_poll(sid, run, res)
        return run

    def _emit_poll(self, sid: str, run: dict[str, Any], res: dict[str, Any]) -> None:
        now = self.clock.time()
        with self.reader() as conn:
            items = store.list_items(conn, ids=res["item_ids"][-200:], limit=200) if res["item_ids"] else []
            ms = store.list_matches(conn, ids=res["match_ids"], limit=500) if res["match_ids"] else []
            row = store.sources(conn)[sid]
        self.emit("watch.poll", run)
        if sid == "meta-adlibrary":
            self.emit("meta.poll", {"run": run})
            if self.meta_worker is not None:
                self.meta_worker.wake()
        for it in reversed(items):
            self.emit("watch.item", {"item": it, "replay": run["replay"]})
        for m in reversed(ms):
            self.emit("watch.match", {"match": m, "replay": run["replay"]})
            if self.auto_assign and not m["replay"] and m["strength"] == "strong":
                self._assign_q.append(m["id"])
        self.emit("watch.source_state", {"source": self.source_view(row, now), "replay": run["replay"]})

    async def step(self) -> list[dict[str, Any]]:
        """One scheduler iteration at clock.time(): poll every due source (sequentially, in a worker thread)."""
        t0 = time.perf_counter()
        runs: list[dict[str, Any]] = []
        if self.state == "live":
            if not self.live_allowed():
                self._set_state("offline")
                return runs
            for sid in await asyncio.to_thread(self.due, self.clock.time()):
                if self.state != "live":
                    break
                try:
                    runs.append(await asyncio.to_thread(self.poll_source, sid))
                except SourceError as e:
                    if e.status == "offline":
                        self._set_state("offline")
                        break
            await self._drain_assign()
        elif self.state == "offline" and self.live_allowed():
            self._set_state("live")
        if self.clock.time() - self._last_housekeep >= HOUSEKEEP_S:
            self._last_housekeep = self.clock.time()
            await asyncio.to_thread(self.housekeep)
        self.metrics.loop_ms.append((time.perf_counter() - t0) * 1000)
        return runs

    async def _drain_assign(self) -> None:
        while self._assign_q:
            mid = self._assign_q.popleft()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.create_incident, mid)

    def _set_state(self, s: str) -> None:
        if self.state != s:
            self.state = s
            self.emit("watch.engine", {"state": s, "state_pl": STATE_PL[s]})

    async def run_forever(self, *, max_iterations: int | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        n = 0
        while self.state in ("live", "offline", "paused"):
            try:
                await self.step()
            except Exception:  # noqa: BLE001 — supervisor: log, keep running
                log.warning("watch-rt step failed", exc_info=True)
            n += 1
            if max_iterations is not None and n >= max_iterations:
                return
            wait = TICK_MAX_S if self.state != "live" else await asyncio.to_thread(self.next_wake, self.clock.time())
            self._wake.clear()
            await self.clock.sleep(max(0.05, min(wait, TICK_MAX_S)), self._wake)

    # ---------------------------------------------------------------- lifecycle
    def _acquire_lock(self) -> bool:
        """One live engine per state dir (API task or `hydra watch run`, never both)."""
        if self._lockfile is not None:
            return True
        import fcntl

        d = Path(self.settings.state_dir) / "watch" / "rt"
        d.mkdir(parents=True, exist_ok=True)
        fh = open(d / "engine.lock", "a+")  # noqa: SIM115 — held for the engine lifetime
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lockfile = fh
        return True

    def _release_lock(self) -> None:
        if self._lockfile is not None:
            with contextlib.suppress(OSError):
                self._lockfile.close()
            self._lockfile = None

    def start(self) -> dict[str, Any]:
        """Start live mode (in the running event loop)."""
        self.stop_replay()
        if not self._acquire_lock():
            raise RuntimeError("Inny proces (hydra watch run) już prowadzi Watch na tym katalogu stanu.")
        self.started_at = store.iso(self.clock.time())
        self._set_state("live" if self.live_allowed() else "offline")
        self._spawn()
        self.start_meta_worker()
        return self.status()

    def start_meta_worker(self) -> Any:
        """META-COLLECT outbox worker in this process (HYDRA_META_WORKER=0 disables it; tests drive it directly)."""
        if os.environ.get("HYDRA_META_WORKER", "1").strip() == "0":
            return None
        if self.meta_worker is None:
            from hydra.meta.analysis import MetaWorker

            self.meta_worker = MetaWorker(self.settings, self.writer, emit=self.emit)
        self.meta_worker.start()
        return self.meta_worker

    def _spawn(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self.run_forever(), name="hydra-watch-rt")

    def pause(self) -> dict[str, Any]:
        if self.state in ("live", "offline"):
            self._set_state("paused")
        return self.status()

    def resume(self) -> dict[str, Any]:
        if self.state == "paused":
            self._set_state("live" if self.live_allowed() else "offline")
            self._spawn()
        return self.status()

    def stop(self) -> dict[str, Any]:
        self.stop_replay()
        self._set_state("stopped")
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._release_lock()
        return self.status()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.stop()
        if self.meta_worker is not None:
            with contextlib.suppress(Exception):
                self.meta_worker.stop()

    # ---------------------------------------------------------------- replay
    def prepare_replay(self, fixture: Fixture, speed: float) -> Replay:
        probes = self._campaign_probes(fixture)
        fetcher = ReplayFetcher(fixture, probes)
        now = self.clock.time()

        def prep(conn: Any) -> None:
            store.clear_replay(conn)
            for e in fixture.manifest.get("watchlist") or []:
                with contextlib.suppress(ValueError):
                    store.add_entry(conn, e["kind"], e["value"], e.get("label"), "replay", now)
            conn.execute("UPDATE watch_sources SET cursor='{}', etag=NULL, last_modified=NULL, "
                         "consecutive_errors=0, circuit_open_until=NULL")

        self.writer.run(prep)
        self.reload_index()
        self.replay = Replay(fixture, max(1.0, min(float(speed), 3600.0)), fetcher, started_at=store.iso(now),
                             probes=sum(len(v) for v in probes.values()))
        return self.replay

    def _campaign_probes(self, fixture: Fixture) -> dict[str, list[str]]:
        """Synthetic CERT-format lines carrying real campaign-derived hosts (local DB only, never sent anywhere):
        shows the matcher against existing campaigns. The items are replay rows (SYNTETYCZNE)."""
        spec = fixture.manifest.get("campaign_probes") or {}
        n = int(spec.get("count") or 0)
        if n <= 0:
            return {}
        with self.reader() as conn:
            rows = conn.execute("SELECT DISTINCT value FROM watch_watchlist WHERE origin='campaign' AND kind='host' "
                                "ORDER BY value LIMIT ?", (n,)).fetchall()
        if not rows:
            return {}
        lines = [json.dumps({"DomainAddress": r[0], "ActionType": "block", "ActionTime": "2026-11-20T04:04:00",
                             "RegisterPositionId": f"probe-{i}", "synthetic_probe": True}) for i, r in enumerate(rows)]
        return {f"{int(spec.get('tick') or 1)}:cert-pl": lines}

    def replay_tick(self, i: int) -> list[dict[str, Any]]:
        assert self.replay is not None
        self.replay.fetcher.set_tick(i)
        runs = []
        for sid in ADAPTERS:
            if sid in self.replay.fetcher.tick or f"{i}:{sid}" in self.replay.fetcher.extra:
                runs.append(self.poll_source(sid, force=True, replay=True))
        self.replay.ticks_done = i + 1
        return runs

    def start_replay(self, fixture: Fixture, speed: float = 60.0) -> dict[str, Any]:
        if self.state == "live" or self.state == "offline" or self.state == "paused":
            self.stop()
        self.prepare_replay(fixture, speed)
        self.started_at = store.iso(self.clock.time())
        self._set_state("replay")
        self.start_meta_worker()
        self._loop = asyncio.get_running_loop()
        self._wake = self._wake or asyncio.Event()
        self._task = self._loop.create_task(self.run_replay(), name="hydra-watch-rt-replay")
        return self.status()

    async def run_replay(self) -> None:
        r = self.replay
        assert r is not None
        interval = float(r.fixture.manifest.get("interval_s") or 60)
        for i in range(len(r.fixture.ticks)):
            if self.state != "replay" or self.replay is not r:
                return
            await asyncio.to_thread(self.replay_tick, i)
            await self.clock.sleep(interval / r.speed)
        r.done = True
        self.emit("watch.engine", {"state": "replay", "state_pl": STATE_PL["replay"], "replay_done": True})

    def stop_replay(self) -> None:
        if self.state == "replay":
            if self._task is not None:
                self._task.cancel()
                self._task = None
            self.replay = None
            self._set_state("stopped")

    # ---------------------------------------------------------------- watchlist / matches
    def rebuild_watchlist(self) -> dict[str, Any]:
        with self.reader() as conn:
            entries = derive(conn)
        res = self.writer.run(lambda c: store.replace_campaign_entries(c, entries, self.clock.time()))
        self.reload_index()
        return res

    def add_entry(self, kind: str, value: str, label: str | None) -> int:
        rid = self.writer.run(lambda c: store.add_entry(c, kind, value, label, "manual", self.clock.time()))
        self.reload_index()
        return rid

    def delete_entry(self, entry_id: int) -> bool:
        ok = self.writer.run(lambda c: store.delete_entry(c, entry_id))
        self.reload_index()
        return ok

    def set_match_status(self, mid: int, status: str) -> dict[str, Any] | None:
        if not self.writer.run(lambda c: store.set_match_status(c, mid, status, self.clock.time())):
            return None
        with self.reader() as conn:
            m = store.list_matches(conn, ids=[mid], limit=1)
        if m:
            self.emit("watch.match", {"match": m[0], "replay": m[0]["replay"]})
        return m[0] if m else None

    def create_incident(self, mid: int) -> dict[str, Any]:
        """Strong match -> candidate incident through the public batch path (`hydra.batch.assign_batch`) ->
        assign / new_campaign / abstain with new_pattern_id. Never for replay rows (synthetic data)."""
        with self.reader() as conn:
            ms = store.list_matches(conn, ids=[mid], limit=1)
            row = conn.execute("SELECT i.kind, i.value FROM watch_matches m JOIN watch_items i ON i.id=m.item_id "
                               "WHERE m.id=?", (mid,)).fetchone()
        if not ms or row is None:
            raise KeyError(mid)
        m = ms[0]
        if m["replay"]:
            raise ValueError("Odtworzenie: z danych syntetycznych nie tworzy się incydentu.")
        kind, value = row
        if kind not in ("url", "domain", "cert"):
            raise ValueError("Incydent tworzy się tylko z elementu z adresem (URL lub host).")
        target = value if kind == "url" else f"https://{value}/"
        from hydra.batch import assign_batch

        try:
            rep = assign_batch([target], self.settings, writer=self.writer)
            it = (rep.get("items") or [{}])[0]
            assign = {"decision": it.get("decision"), "campaign_id": it.get("campaign_id"),
                      "new_pattern_id": it.get("new_pattern_id"), "batch_run": rep.get("run_id"),
                      "incident_id": it.get("incident_id"), "message_pl": it.get("reason_pl")}
        except Exception as exc:  # noqa: BLE001 — e.g. no base run yet
            assign = {"decision": "error", "message_pl": f"{type(exc).__name__}: {str(exc)[:300]}"}
        self.writer.run(lambda c: store.set_match_assign(c, mid, assign, self.clock.time()))
        with self.reader() as conn:
            m = store.list_matches(conn, ids=[mid], limit=1)[0]
        self.emit("watch.match", {"match": m, "replay": False})
        return {"match": m, "assign": assign}

    def set_source(self, sid: str, *, enabled: bool | None = None, interval_s: int | None = None) -> dict[str, Any]:
        if sid not in ADAPTERS:
            raise KeyError(sid)
        a = ADAPTERS[sid]
        if interval_s is not None:
            interval_s = max(int(interval_s), effective_min_interval(a))
        now = self.clock.time()
        self.writer.run(lambda c: store.set_source_config(c, sid, now, enabled=enabled, interval_s=interval_s))
        if enabled:
            # the watch toggle is the operator's explicit consent for this connector (integrations enabled flag)
            from hydra.integrations import state as istate

            istate.set_enabled(self.settings, a.connector_id, True)
        with self.reader() as conn:
            row = store.sources(conn)[sid]
        v = self.source_view(row, now)
        self.emit("watch.source_state", {"source": v, "replay": False})
        if self._wake is not None and self._loop is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._wake.set)
        return v

    def housekeep(self) -> dict[str, int]:
        days = int(os.environ.get("HYDRA_WATCH_RETENTION_DAYS") or 30)
        rdays = int(os.environ.get("HYDRA_WATCH_RUNS_RETENTION_DAYS") or 14)
        out = self.writer.run(lambda c: store.retention(c, self.clock.time(), item_days=days, run_days=rdays))
        from hydra.meta import store as mstore  # META-COLLECT: sightings / poll rows age out; evidence is kept

        with contextlib.suppress(Exception):
            self.last_meta_retention = self.writer.run(lambda c: mstore.retention(
                c, self.clock.time(), observation_days=days, poll_days=days) if mstore.has_schema(c) else {})
        return out

    def measurements(self) -> dict[str, Any]:
        def pct(xs: list[float], p: float) -> float | None:
            if not xs:
                return None
            s = sorted(xs)
            return round(s[min(len(s) - 1, int(p * len(s)))], 3)

        lm, mu = list(self.metrics.loop_ms), list(self.metrics.match_us)
        return {"loop_ms_p50": pct(lm, 0.5), "loop_ms_p95": pct(lm, 0.95), "match_us_p50": pct(mu, 0.5),
                "match_us_p95": pct(mu, 0.95), "samples": {"loop": len(lm), "match": len(mu)}}


_LOCK = threading.Lock()


def for_app(app_state: Any, holder: Any) -> WatchRT:
    """The process engine of one FastAPI app (`app.state.watch_rt`)."""
    with _LOCK:
        eng = getattr(holder, "watch_rt", None)
        if eng is None:
            eng = WatchRT(app_state.settings, app_state.writer)
            holder.watch_rt = eng
        hub = getattr(holder, "stream_hub", None)  # live stream already running: forward this engine's events
        if hub is not None and getattr(hub, "watch_rt", None) is not eng:
            hub.watch_rt, hub._rt_seq = eng, 0
        return eng
