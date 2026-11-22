"""HYDRA Watch engine: sources (live follow of the recording / replay) → score → families → enrich → assign.

Modes:
- `live`: follows the newest `certpl-<date>.jsonl` written by the recorder (a separate process, see
  recorder.py). With `HYDRA_WATCH_POLL=1` and HYDRA_ENRICH=1 the engine also runs the CERT tail in-process.
- `replay`: re-emits a recording at `speed`× (default 60) on the same code path, paced by source time
  (`first_seen`); gaps are capped so a quiet hour does not stall a demo. Every replayed event carries
  `replay=True` and the status shows the REPLAY flag, file, sha256 and speed.
Before a run the family index is seeded with recorded history strictly older than the first event of the run,
so "family of N listed domains" is what an analyst would have known at that moment.

No page is ever fetched here (safety default). `fetch(event_id)` is the analyst-click path (api_watch).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
from collections import Counter, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.watch.enrich import RateLimit, enrich_host, infra_keys
from hydra.watch.events import WatchEvent, normalise
from hydra.watch.families import FamilyIndex, LiveCampaigns
from hydra.watch.link import Linker, shape_is_rare
from hydra.watch.score import A_CUT, score_host

MAX_EVENTS = int(os.environ.get("HYDRA_WATCH_MAX_EVENTS", "5000"))
MAX_GAP_S = 3.0  # replay: longest real-time sleep between two events
LIVE_PATTERNS = ("certpl-2*.jsonl", "ct-2*.jsonl")  # recorder outputs followed in live mode


def recordings_dir(settings: Settings) -> Path:
    env = os.environ.get("HYDRA_WATCH_REC_DIR")
    return Path(env).expanduser() if env else Path(settings.data_dir) / "watch" / "recordings"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_recording(path: Path, stats: dict[str, int] | None = None) -> list[WatchEvent]:
    """Events of a JSONL recording. Lines that are not JSON or not a watch record are skipped and counted in
    `stats["malformed"]` (CR-ASTRA-PIPE-310: a malformed line is an input error, never a server error)."""
    out: list[WatchEvent] = []
    bad = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.extend(normalise(json.loads(line)))
            except (ValueError, TypeError):
                bad += 1
    if stats is not None:
        stats["malformed"] = stats.get("malformed", 0) + bad
    return out


def list_recordings(settings: Settings) -> list[dict[str, Any]]:
    d = recordings_dir(settings)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.jsonl")):
        with p.open("rb") as fh:
            n = sum(1 for _ in fh)
        out.append({"file": p.name, "lines": n, "bytes": p.stat().st_size,
                    "modified": datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat(timespec="seconds")})
    return out


class WatchEngine:
    def __init__(self, settings: Settings, *, corpus: str | None = None, enrich: bool = True,
                 assign: bool = True) -> None:
        self.settings = settings
        self.corpus = corpus or os.environ.get("HYDRA_WATCH_CORPUS", "nask")
        self.do_enrich, self.do_assign = enrich, assign
        self.linker = Linker(settings, self.corpus)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._work: queue.Queue[tuple[WatchEvent, bool]] = queue.Queue(maxsize=1000)
        self._rate = RateLimit()
        self._reset()
        self.mode = "idle"
        self.replay_info: dict[str, Any] | None = None
        self.index_loaded = False
        self._blocking = False
        self.fingerprints = None  # fingerprints.FingerprintSet, built with the campaign index
        self.fp_exclude: set[str] = set()

    # ------------------------------------------------------------------ state
    def _reset(self) -> None:
        self.events: deque[WatchEvent] = deque(maxlen=MAX_EVENTS)
        self.by_id: dict[str, WatchEvent] = {}
        self.seq = 0
        self.families = FamilyIndex()
        self.live = LiveCampaigns()
        self.counters: Counter[str] = Counter()
        self.per_minute: Counter[str] = Counter()
        self.clock: str | None = None  # source time of the newest event
        self.started_at = _now()
        self.seeded = 0
        self._seen_keys: set[str] = set()
        self.alerts: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.alert_seq = 0
        self.first_watch: dict[str, tuple[str, str]] = {}  # registrable -> (first_seen, source) of non-CERT sightings
        self.lead: list[dict[str, Any]] = []

    def ensure_index(self) -> None:
        if not self.index_loaded and self.do_assign:
            self.linker.reload()
            self.index_loaded = True
            self.build_fingerprints()

    def build_fingerprints(self) -> None:
        from hydra.watch import fingerprints as F

        try:
            self.fingerprints = F.build(self.linker.index, self.fp_exclude)
            F.save(self.fingerprints, self._state_dir())
        except Exception:  # noqa: BLE001 - alerts are optional
            self.fingerprints = None

    def _state_dir(self) -> Path:
        env = os.environ.get("HYDRA_WATCH_STATE_DIR")
        assert self.settings.state_dir is not None
        return Path(env).expanduser() if env else self.settings.state_dir / "watch"

    def _alert(self, ev: WatchEvent, enrichment: dict | None) -> None:
        from hydra.watch import fingerprints as F

        if self.fingerprints is None:
            return
        found = F.match(self.fingerprints, ev.host, enrichment, self.families)
        if not found:
            return
        new = ev.alerts is None
        ev.alerts = found
        with self._lock:
            if new:
                self.alert_seq += 1
                self.counters["alerts"] += 1
                self.counters[f"alerts_{found[0]['level']}"] += 1
                self.alerts.append({"alert_seq": self.alert_seq, "event_id": ev.event_id})
            reg = (ev.score or {}).get("registrable")
            if reg and ev.source != "watch:certpl":
                self.first_watch.setdefault(reg, (ev.first_seen, ev.source))

    def seed(self, until: str | None, exclude: Path | None = None) -> int:
        """Feed recorded history older than `until` into the family index and live campaigns (no ticker rows)."""
        d = recordings_dir(self.settings)
        if not d.is_dir():
            return 0
        evs: dict[str, WatchEvent] = {}
        for p in sorted(d.glob("certpl*.jsonl")):
            if exclude is not None and p.resolve() == exclude.resolve():
                continue
            for e in read_recording(p):
                if e.action == "block" and (until is None or e.first_seen < until):
                    evs.setdefault(e.event_id, e)
        n = 0
        for e in sorted(evs.values(), key=lambda x: x.first_seen):
            self._index(e)
            self._seen_keys.add(e.event_id)
            n += 1
        self.seeded = n
        return n

    def _index(self, ev: WatchEvent, enrichment: dict | None = None) -> tuple[Any, str | None]:
        from hydra.enrich.domains import url_label_skeleton
        from hydra.watch.names import parts

        s = score_host(ev.host)  # family/skeleton keys only (no context needed)
        subs = tuple(sorted(set(parts(ev.host).f_sub_labels or [])))
        self.families.add(ev.first_seen, s.family, s.registrable, url_label_skeleton(s.host), subs)
        lc = self.live.add(s.registrable, s.family, infra_keys(enrichment), host=s.host)
        return s, lc

    # ------------------------------------------------------------------ processing
    def process(self, ev: WatchEvent) -> WatchEvent | None:
        with self._lock:
            if ev.event_id in self._seen_keys or ev.event_id in self.by_id:
                return None
            if ev.action != "block" and ev.source == "watch:certpl":
                self.counters["unblock"] += 1
                return None
            self.ensure_index()
            idx = self.linker.index if self.do_assign else None
            sc = score_host(ev.host, families=self.families, campaigns=idx)
            _s, lc = self._index(ev)
            # scored before indexing: the family reason counts the *other* listed members
            ev.score = sc.to_dict()
            ev.live_campaign = lc
            self.seq += 1
            ev.seq = self.seq
            self.events.append(ev)
            self.by_id[ev.event_id] = ev
            self._lead(ev)
            if len(self.by_id) > MAX_EVENTS * 2:
                keep = {e.event_id for e in self.events}
                self.by_id = {k: v for k, v in self.by_id.items() if k in keep}
            self.clock = max(self.clock or ev.first_seen, ev.first_seen)
            self.per_minute[ev.first_seen[:16]] += 1
            self.counters["events"] += 1
            self.counters[f"source:{ev.source}"] += 1
            if sc.high:
                self.counters["high"] += 1
            if lc:
                self.counters["in_live_campaign"] += 1
            if sc.campaign_hints:
                self.counters["campaign_gene_hits"] += 1
            if any(r.code == "campaign_family" for r in sc.reasons):
                self.counters["family_link_known"] += 1
        if self.do_enrich or self.do_assign:
            # enrichment only for candidates (high score, live campaign, campaign gene); assignment for all
            ev.enrichment = None
            item = (ev, bool(sc.high or lc or sc.campaign_hints))
            try:
                if self._blocking:  # measurement replays: never drop, wait for the worker
                    self._work.put(item)
                else:
                    self._work.put_nowait(item)
                self.counters["queued"] += 1
            except queue.Full:
                self.counters["dropped"] += 1
        self._alert(ev, None)
        self._log(ev)
        return ev

    def _lead(self, ev: WatchEvent) -> None:
        """Lead time: a CERT listing of a registrable domain that Watch saw earlier on another source (CT)."""
        reg = (ev.score or {}).get("registrable")
        if not reg:
            return
        if ev.source != "watch:certpl":
            self.first_watch.setdefault(reg, (ev.first_seen, ev.source))
            return
        prior = self.first_watch.get(reg)
        if prior:
            delta = (_dt(ev.first_seen) - _dt(prior[0])).total_seconds() / 3600
            self.lead.append({"registrable": reg, "watch_first_seen": prior[0], "watch_source": prior[1],
                              "cert_listed": ev.first_seen, "lead_h": round(delta, 2)})

    def _post(self, ev: WatchEvent, enrich: bool = True) -> None:
        enrichment = None
        if enrich and self.do_enrich:
            if self.settings.enrich:  # live lookups only; cache replay is not rate-limited
                self._rate.wait()
            try:
                enrichment = enrich_host(ev.host, ev.first_seen, float((ev.score or {}).get("bits", 0)),
                                         self.settings)
                self.counters["enriched"] += 1
            except Exception as exc:  # noqa: BLE001 - enrichment failure = unavailable
                enrichment = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            ev.enrichment = enrichment
            self._alert(ev, enrichment)
            cp = (enrichment or {}).get("cert_pl") or {}
            if ev.source != "watch:certpl" and cp.get("insert_date"):
                try:
                    delta = (_dt(str(cp["insert_date"])) - _dt(ev.first_seen)).total_seconds() / 3600
                    self.lead.append({"registrable": (ev.score or {}).get("registrable"), "watch_first_seen":
                                      ev.first_seen, "watch_source": ev.source, "cert_listed": cp["insert_date"],
                                      "lead_h": round(delta, 2), "via": "cert_pl_snapshot"})
                except (ValueError, TypeError):
                    pass
            keys = infra_keys(enrichment)
            if keys and ev.score:
                with self._lock:
                    lc = self.live.add(ev.score["registrable"], ev.score["family"], keys)
                    if lc and not ev.live_campaign:
                        ev.live_campaign = lc
                        self.counters["in_live_campaign"] += 1
        if self.do_assign:
            try:
                from hydra.enrich.domains import url_label_skeleton

                sk = url_label_skeleton(ev.host)
                n_sk, n_all = self.families.skeleton_freq(sk) if sk else (0, 0)
                a = self.linker.assign(ev.event_id, ev.host, enrichment, skeleton_ok=shape_is_rare(n_sk, n_all))
                a["shape_background"] = {"skeleton": sk, "count": n_sk, "names": n_all}
            except Exception as exc:  # noqa: BLE001
                a = {"decision": "unavailable", "reason_pl": f"Błąd przypisania: {type(exc).__name__}"}
            ev.assignment = a
            self.counters[f"decision:{a.get('decision')}"] += 1
            if a.get("decision") == "assign":
                self.counters["assigned_known"] += 1

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                ev, want_enrich = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._post(ev, enrich=want_enrich)
            finally:
                self._work.task_done()

    def _log(self, ev: WatchEvent) -> None:
        env = os.environ.get("HYDRA_WATCH_STATE_DIR")
        assert self.settings.state_dir is not None
        d = Path(env).expanduser() if env else self.settings.state_dir / "watch"
        d.mkdir(parents=True, exist_ok=True)
        name = f"processed-{'replay' if ev.replay else 'live'}-{_now()[:10]}.jsonl"
        with (d / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": ev.seq, "event_id": ev.event_id, "host": ev.host, "source": ev.source,
                                 "first_seen": ev.first_seen, "bits": (ev.score or {}).get("bits"),
                                 "reasons": [r["code"] for r in (ev.score or {}).get("reasons", [])],
                                 "family": (ev.score or {}).get("family"), "live_campaign": ev.live_campaign,
                                 "hints": (ev.score or {}).get("campaign_hints"), "replay": ev.replay},
                                ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ runs
    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []
        self._stop = threading.Event()
        self._work = queue.Queue(maxsize=1000)
        self.mode = "idle"

    def _start_workers(self) -> None:
        t = threading.Thread(target=self._worker, name="watch-post", daemon=True)
        t.start()
        self._threads.append(t)

    def start_replay(self, file: str, speed: float = 60.0, *, block: bool = False) -> dict[str, Any]:
        d = recordings_dir(self.settings)
        p = (d / file).resolve()
        if p.parent != d.resolve() or not p.is_file() or p.suffix != ".jsonl":
            raise FileNotFoundError(file)
        self.stop()
        with self._lock:
            self._reset()
        stats: dict[str, int] = {}
        evs = sorted({e.event_id: e for e in read_recording(p, stats)}.values(), key=lambda e: e.first_seen)
        evs = [e for e in evs if e.action != "unblock"]
        if not evs:
            if stats.get("malformed"):
                raise ValueError(f"Nagranie {p.name} nie zawiera żadnego poprawnego zdarzenia: "
                                 f"{stats['malformed']} uszkodzonych rekordów (zła struktura JSON).")
            raise ValueError(f"Nagranie {p.name} jest puste: brak zdarzeń do odtworzenia (empty recording).")
        self.ensure_index()
        self.seed(evs[0].first_seen, exclude=p)
        speed = max(1.0, min(float(speed), 36000.0))
        self.replay_info = {"file": p.name, "speed": speed, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                            "events": len(evs), "window": [evs[0].first_seen, evs[-1].first_seen], "emitted": 0,
                            "done": False, "started_at": _now()}
        self.mode = "replay"
        self._start_workers()

        def run() -> None:
            prev: datetime | None = None
            for e in evs:
                if self._stop.is_set():
                    return
                t = _dt(e.first_seen)
                if prev is not None:
                    gap = (t - prev).total_seconds() / speed
                    if gap > 0:
                        self._stop.wait(min(gap, MAX_GAP_S))
                prev = t
                e.replay = True
                self.process(e)
                self.replay_info["emitted"] += 1
            self.replay_info["done"] = True

        if block:
            self._blocking = True
            try:
                run()
            finally:
                self._blocking = False
            self._work.join()
            return self.status()
        t = threading.Thread(target=run, name="watch-replay", daemon=True)
        t.start()
        self._threads.append(t)
        return self.status()

    def start_live(self, recent: int = 30) -> dict[str, Any]:
        self.stop()
        with self._lock:
            self._reset()
        self.ensure_index()
        d = recordings_dir(self.settings)
        d.mkdir(parents=True, exist_ok=True)
        self.replay_info = None
        self.mode = "live"
        files = sorted(p for p in d.glob("certpl-2*.jsonl"))
        hist = []
        if files:
            hist = sorted({e.event_id: e for e in read_recording(files[-1])}.values(), key=lambda e: e.first_seen)
        cutoff = hist[-recent].first_seen if len(hist) >= recent else (hist[0].first_seen if hist else None)
        self.seed(cutoff)
        self._start_workers()
        for e in hist[-recent:]:
            self.process(e)
        from hydra.enrich.client import live_enabled

        if os.environ.get("HYDRA_WATCH_POLL") == "1" and live_enabled(self.settings):  # HYDRA_OFFLINE=1 wins
            from hydra.watch.recorder import CertTail

            tail = CertTail(d)

            def poll() -> None:
                while not self._stop.is_set():
                    try:
                        tail.poll()
                    except Exception:  # noqa: BLE001
                        pass
                    self._stop.wait(90)

            tp = threading.Thread(target=poll, name="watch-certpl-poll", daemon=True)
            tp.start()
            self._threads.append(tp)

        def follow() -> None:
            # one cursor per source file family; CERT starts at the end of today's file (recent rows shown above)
            cursors: dict[str, tuple[Path | None, int]] = {}
            for pat in LIVE_PATTERNS:
                newest = max(d.glob(pat), default=None)
                cursors[pat] = (newest, newest.stat().st_size if newest else 0)
            while not self._stop.is_set():
                for pat, (cur, pos) in list(cursors.items()):
                    newest = max(d.glob(pat), default=None)
                    if newest is not None and newest != cur:
                        cur, pos = newest, 0
                    if cur is not None and cur.stat().st_size > pos:
                        with cur.open("rb") as fh:
                            fh.seek(pos)
                            chunk = fh.read()
                        last_nl = chunk.rfind(b"\n")
                        if last_nl >= 0:
                            pos += last_nl + 1
                            for line in chunk[: last_nl + 1].decode("utf-8", "replace").splitlines():
                                try:
                                    for e in normalise(json.loads(line)):
                                        self.process(e)
                                except (ValueError, TypeError):
                                    continue
                    cursors[pat] = (cur, pos)
                self._stop.wait(2.0)

        t = threading.Thread(target=follow, name="watch-live", daemon=True)
        t.start()
        self._threads.append(t)
        return self.status()

    # ------------------------------------------------------------------ views
    def events_since(self, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            evs = [e for e in self.events if e.seq > since][-limit:]
            out = []
            for e in evs:
                d = e.to_dict()
                reg = (e.score or {}).get("registrable")
                # a burst becomes a live campaign after its first member: show the current link
                d["live_campaign"] = (self.live.campaign_of(reg) if reg else None) or e.live_campaign
                out.append(d)
        return out

    def alerts_since(self, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            refs = [a for a in self.alerts if a["alert_seq"] > since][-limit:]
            out = []
            for a in refs:
                ev = self.by_id.get(a["event_id"])
                if ev is None or not ev.alerts:
                    continue
                out.append({"alert_seq": a["alert_seq"], "event_id": ev.event_id, "seq": ev.seq, "source": ev.source,
                            "host_defanged": ev.host_defanged, "first_seen": ev.first_seen, "replay": ev.replay,
                            "event_bits": (ev.score or {}).get("bits"), "assignment": (ev.assignment or {}).get(
                                "decision"), "alerts": ev.alerts})
        return out

    def sparkline(self, minutes: int = 60) -> list[dict[str, Any]]:
        if not self.clock:
            return []
        end = _dt(self.clock).replace(second=0, microsecond=0)
        out = []
        for i in range(minutes - 1, -1, -1):
            m = (end - timedelta(minutes=i)).isoformat(timespec="minutes")[:16]
            out.append({"minute": m, "n": self.per_minute.get(m, 0)})
        return out

    def status(self) -> dict[str, Any]:
        c = self.counters
        n = c.get("events", 0)
        return {"mode": self.mode, "replay": self.replay_info, "clock": self.clock, "started_at": self.started_at,
                "events": n, "high": c.get("high", 0), "share_high": round(c.get("high", 0) / n, 3) if n else None,
                "a_cut_bits": A_CUT, "in_live_campaign": c.get("in_live_campaign", 0),
                "campaign_gene_hits": c.get("campaign_gene_hits", 0), "assigned_known": c.get("assigned_known", 0),
                "family_link_known": c.get("family_link_known", 0),
                "decisions": {k.split(":", 1)[1]: v for k, v in c.items() if k.startswith("decision:")},
                "alerts": c.get("alerts", 0), "alerts_campaign": c.get("alerts_campaign", 0),
                "alerts_group": c.get("alerts_group", 0),
                "fingerprints": self.fingerprints.summary() if self.fingerprints is not None else None,
                "lead_time": self.lead[-20:],
                "enriched": c.get("enriched", 0), "queued": c.get("queued", 0), "dropped": c.get("dropped", 0),
                "pending": self._work.qsize(), "seeded_history": self.seeded,
                "live_enrichment": bool(self.settings.enrich), "corpus": self.corpus,
                "campaign_index": self.linker.index.summary(), "families": self.families.stats(),
                "live_campaigns": self.live.count(), "top_families": self.families.top(8),
                "top_live_campaigns": self.live.summary(8), "sparkline": self.sparkline(60),
                "recordings_dir": str(recordings_dir(self.settings))}
