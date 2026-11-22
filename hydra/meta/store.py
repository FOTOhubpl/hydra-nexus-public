"""META-COLLECT persistence (schema v8, hydra/db.py). Writer-side functions take the Writer's connection (they run
inside `Writer.run`, one transaction); readers take a read-only connection. No other write connection is opened.

Identifier spaces (never mixed): `ad_id` = Ad Library id (string), `revision id` = our row id, `incident_id` =
HYDRA incident (`metaad-<mode>-<ad>-<sha8>`), `hydra_campaign_id` = a campaign of the base run (or NULL).
Every row carries `mode` (live | recorded | synthetic): replay and live never overwrite each other.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from hydra import events
from hydra.meta import normalize

MODES = ("live", "recorded", "synthetic")
MODE_PL = {"live": "NA ŻYWO", "recorded": "ODTWORZENIE", "synthetic": "DANE SYNTETYCZNE"}
EXTRACT_VERSION = "meta-collect@1"
EVENT_CONFIG = "META_CONFIG_CHANGED"
EVENT_POLL = "META_POLL"
EVENT_ANALYSIS = "META_ANALYSIS"
EVENT_REVIEW = "META_REVIEW"
EVENT_ATTACH = "META_ATTACH"

COLLECTION_PL = {
    "metadata_available": "Metadane dostępne (tekst, strona, daty); materiały nie zostały jeszcze sprawdzone.",
    "asset_discovery_pending": "Oczekuje na sprawdzenie dostępnych materiałów.",
    "asset_available": "Materiał pozyskany i zapisany (CAS).",
    "asset_unavailable": "Materiał niedostępny.",
    "blocked_or_login_required": "Podgląd zablokowany albo wymaga logowania lub JavaScriptu; materiału nie pobrano.",
    "budget_deferred": "Odroczono z powodu limitu (budżet żądań lub bajtów).",
    "invalid_or_unsupported_media": "Plik odrzucony: zły typ, uszkodzony, za duży albo spoza dozwolonych hostów.",
}
OUTBOX_PL = {"queued": "w kolejce", "running": "w toku", "done": "zakończona", "partial": "częściowa",
             "failed": "błąd", "deferred": "odroczona (limit)",
             "superseded": "zastąpiona równoważnym zadaniem"}

DEFAULT_CONFIG: dict[str, Any] = {
    "countries": ["PL"],
    "ad_active_status": "ALL",
    "languages": [],  # optional, deliberate filter: empty by default (a `pl` filter narrows Polish reach)
    "date_window_days": 7,
    "overlap_days": 2,
    "budget_per_h": 60,
    "max_active_queries": 10,
    "max_pages_per_query": 2,
    "page_size": 50,
    "interval_s": 1800,
    "max_analyses_per_h": 300,  # live 2026-11-22: ~160 new ads per cycle of 10 queries x 1 page of 25
    "max_media_bytes": 25_000_000,
    "max_media_per_ad": 4,
    "max_media_total_bytes": 2_000_000_000,  # disk cap for downloaded Meta media (snapshot / CDN), all modes
    "media_fetch": False,
    "landing_fetch": False,
    "expansion": {"enabled": False, "max_depth": 1, "max_pages": 5},
    # BONUS-AUTO search rules (docs/integrations/META_COLLECTION.md §2a). Sent to Meta: date_from / date_to
    # (ad_delivery_date_min / _max; the rolling window "ostatnie N dni" is date_window_days) and platforms
    # (publisher_platforms). Applied locally by the triage (never sent anywhere): rules.*.
    "date_from": None,
    "date_to": None,
    "platforms": [],
    "rules": {"keywords": [], "celebrities": [], "page_name_patterns": [], "same_text_min_pages": 3,
              "alert_min_priority": "medium"},
    "config_version": 1,
}
PLATFORMS = ("FACEBOOK", "INSTAGRAM", "AUDIENCE_NETWORK", "MESSENGER", "WHATSAPP", "OCULUS", "THREADS")
ALERT_LEVELS = {"low": ("low", "medium", "high"), "medium": ("medium", "high"), "high": ("high",)}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Migration v8 statements (IF NOT EXISTS) for a DB whose user_version passed 8 before this branch."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta_outbox'").fetchone():
        return False
    from hydra.db import MIGRATIONS, _split

    for statement in _split(dict(MIGRATIONS)[8]):
        conn.execute(statement)
    return True


def has_schema(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta_outbox'").fetchone())


# ------------------------------------------------------------------------------------------------ state / config
def get_state(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    r = conn.execute("SELECT value FROM meta_state WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def set_state(conn: sqlite3.Connection, key: str, value: Any, now: float) -> None:
    conn.execute("INSERT INTO meta_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value, updated_at=excluded.updated_at",
                 (key, json.dumps(value, ensure_ascii=False, default=str), iso(now)))


def config(conn: sqlite3.Connection) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    stored = get_state(conn, "config", {}) or {}
    for k, v in stored.items():
        if k in ("expansion", "rules") and isinstance(v, dict):
            cfg[k].update(v)
        elif k in cfg:
            cfg[k] = v
    cfg["queries"] = list_queries(conn)
    return cfg


def query_id(kind: str, value: str) -> str:
    return "q-" + hashlib.sha256(f"{kind}|{value}".encode()).hexdigest()[:12]


def _clean_query(q: dict[str, Any]) -> tuple[str, str]:
    kind = str(q.get("kind") or "keyword")
    if kind not in ("keyword", "page_ids"):
        raise ValueError("Rodzaj zapytania: keyword albo page_ids.")
    if kind == "keyword":
        value = " ".join(str(q.get("value") or "").split())
        if not value or len(value) > 100:
            raise ValueError("Hasło: 1–100 znaków (limit Meta dla search_terms).")
    else:
        ids = [p.strip() for p in str(q.get("value") or "").replace(";", ",").split(",") if p.strip()]
        if not ids or len(ids) > 10 or not all(p.isdigit() for p in ids):
            raise ValueError("Strony: od 1 do 10 numerycznych identyfikatorów page_id, rozdzielonych przecinkami.")
        value = ",".join(ids)
    return kind, value


INT_KEYS = {"date_window_days": (1, 90), "overlap_days": (0, 14), "budget_per_h": (1, 600),
            "max_active_queries": (1, 50), "max_pages_per_query": (1, 10), "page_size": (5, 100),
            "interval_s": (600, 86400), "max_analyses_per_h": (0, 600), "max_media_bytes": (0, 200_000_000),
            "max_media_per_ad": (0, 20), "max_media_total_bytes": (0, 100_000_000_000)}


def put_config(conn: sqlite3.Connection, patch: dict[str, Any], now: float, actor: str = "api") -> dict[str, Any]:
    cur = get_state(conn, "config", {}) or {}
    changed: list[str] = []
    for k, (lo, hi) in INT_KEYS.items():
        if k in patch and patch[k] is not None:
            v = int(patch[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k}: dozwolony zakres {lo}–{hi}.")
            cur[k] = v
            changed.append(k)
    if patch.get("countries") is not None:
        cs = [str(c).strip().upper() for c in patch["countries"] if str(c).strip()]
        if not cs or not all(len(c) == 2 and c.isalpha() for c in cs):
            raise ValueError("Kraje: dwuliterowe kody ISO (np. PL).")
        cur["countries"] = cs[:10]
        changed.append("countries")
    if patch.get("languages") is not None:
        ls = [str(x).strip().lower() for x in patch["languages"] if str(x).strip()]
        if not all(2 <= len(x) <= 3 and x.isalpha() for x in ls):
            raise ValueError("Języki: kody ISO 639-1/639-3.")
        cur["languages"] = ls[:10]
        changed.append("languages")
    if patch.get("ad_active_status") is not None:
        if patch["ad_active_status"] not in ("ALL", "ACTIVE", "INACTIVE"):
            raise ValueError("ad_active_status: ALL, ACTIVE albo INACTIVE.")
        cur["ad_active_status"] = patch["ad_active_status"]
        changed.append("ad_active_status")
    for k in ("media_fetch", "landing_fetch"):
        if patch.get(k) is not None:
            cur[k] = bool(patch[k])
            changed.append(k)
    if isinstance(patch.get("expansion"), dict):
        e = {**DEFAULT_CONFIG["expansion"], **(cur.get("expansion") or {})}
        for k, v in patch["expansion"].items():
            if k == "enabled":
                e["enabled"] = bool(v)
            elif k in ("max_depth", "max_pages"):
                e[k] = max(0, min(int(v), 3 if k == "max_depth" else 50))
        cur["expansion"] = e
        changed.append("expansion")
    for k in ("date_from", "date_to"):
        if k in patch:
            v = patch[k]
            if v in (None, ""):
                cur[k] = None
            else:
                try:
                    v = date.fromisoformat(str(v)).isoformat()
                except ValueError as exc:
                    raise ValueError(f"{k}: data w formacie RRRR-MM-DD.") from exc
                cur[k] = v
            changed.append(k)
    if cur.get("date_from") and cur.get("date_to") and cur["date_from"] > cur["date_to"]:
        raise ValueError("Data od nie może być późniejsza niż data do.")
    if patch.get("platforms") is not None:
        ps = sorted({str(x).strip().upper() for x in patch["platforms"] if str(x).strip()})
        if any(x not in PLATFORMS for x in ps):
            raise ValueError("Platformy: " + ", ".join(PLATFORMS) + ".")
        cur["platforms"] = ps
        changed.append("platforms")
    if isinstance(patch.get("rules"), dict):
        r = {**DEFAULT_CONFIG["rules"], **(cur.get("rules") or {})}
        for k, v in patch["rules"].items():
            if k in ("keywords", "celebrities", "page_name_patterns") and v is not None:
                items = [" ".join(str(x).split())[:80] for x in v if str(x).strip()]
                if len(items) > 50:
                    raise ValueError(f"{k}: najwyżej 50 pozycji.")
                if k == "page_name_patterns":
                    for x in items:
                        try:
                            re.compile(x, re.I)
                        except re.error as exc:
                            raise ValueError(f"Wzorzec nazwy strony „{x}”: niepoprawne wyrażenie.") from exc
                r[k] = list(dict.fromkeys(items))
            elif k == "same_text_min_pages" and v is not None:
                if not 2 <= int(v) <= 20:
                    raise ValueError("same_text_min_pages: dozwolony zakres 2–20.")
                r[k] = int(v)
            elif k == "alert_min_priority" and v is not None:
                if v not in ALERT_LEVELS:
                    raise ValueError("alert_min_priority: low, medium albo high.")
                r[k] = v
        cur["rules"] = r
        changed.append("rules")
    if patch.get("queries") is not None:
        replace_queries(conn, patch["queries"], now, int(cur.get("max_active_queries") or 10))
        changed.append("queries")
    if changed:
        cur["config_version"] = int(cur.get("config_version") or 1) + 1
        set_state(conn, "config", cur, now)
        events.append(conn, EVENT_CONFIG, {"changed": sorted(set(changed)), "actor": actor,
                                           "config_version": cur["config_version"]})
    return config(conn)


def replace_queries(conn: sqlite3.Connection, queries: list[dict[str, Any]], now: float, max_active: int) -> None:
    keep: set[str] = set()
    n_active = 0
    for q in queries:
        kind, value = _clean_query(q)
        qid = query_id(kind, value)
        enabled = bool(q.get("enabled", True))
        n_active += int(enabled)
        if n_active > max_active and enabled:
            raise ValueError(f"Najwyżej {max_active} aktywnych zapytań.")
        keep.add(qid)
        conn.execute("INSERT INTO meta_queries (id, kind, value, label, origin, enabled, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET label=excluded.label, "
                     "enabled=excluded.enabled, updated_at=excluded.updated_at",
                     (qid, kind, value, (str(q.get("label") or "")[:200] or None), q.get("origin") or "manual",
                      int(enabled), iso(now), iso(now)))
    for (qid, origin) in conn.execute("SELECT id, origin FROM meta_queries").fetchall():
        if qid not in keep and origin != "expansion":
            conn.execute("DELETE FROM meta_queries WHERE id=?", (qid,))


def add_query(conn: sqlite3.Connection, kind: str, value: str, now: float, *, origin: str, label: str | None = None,
              depth: int = 0, parent_ad_id: str | None = None, enabled: bool = True) -> str:
    kind, value = _clean_query({"kind": kind, "value": value})
    qid = query_id(kind, value)
    conn.execute("INSERT OR IGNORE INTO meta_queries (id, kind, value, label, origin, enabled, depth, parent_ad_id, "
                 "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (qid, kind, value, label, origin, int(enabled), depth, parent_ad_id, iso(now), iso(now)))
    return qid


QCOLS = ("id", "kind", "value", "label", "origin", "enabled", "config_version", "depth", "parent_ad_id", "cursor",
         "last_run", "last_ok", "last_status", "last_error", "created_at", "updated_at")


def list_queries(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = f"SELECT {', '.join(QCOLS)} FROM meta_queries" + (" WHERE enabled=1" if enabled_only else "")
    out = []
    for r in conn.execute(sql + " ORDER BY origin DESC, created_at, id"):
        d = dict(zip(QCOLS, r, strict=True))
        d["enabled"] = bool(d["enabled"])
        d["cursor"] = json.loads(d["cursor"] or "{}")
        out.append(d)
    return out


def set_query_cursor(conn: sqlite3.Connection, qid: str, mode: str, cursor: dict[str, Any] | None, now: float, *,
                     status: str, error: str | None = None, ok: bool = False) -> None:
    r = conn.execute("SELECT cursor FROM meta_queries WHERE id=?", (qid,)).fetchone()
    if r is None:
        return
    cur = json.loads(r[0] or "{}")
    if cursor is None:
        cur.pop(mode, None)
    else:
        cur[mode] = cursor
    conn.execute("UPDATE meta_queries SET cursor=?, last_run=?, last_status=?, last_error=?, "
                 "last_ok=COALESCE(?, last_ok), updated_at=? WHERE id=?",
                 (json.dumps(cur, ensure_ascii=False), iso(now), status, error, iso(now) if ok else None, iso(now),
                  qid))


# ------------------------------------------------------------------------------------------------ polls
def start_poll(conn: sqlite3.Connection, mode: str, query: str | None, trigger: str, now: float,
               cursor_before: Any = None) -> int:
    c = conn.execute("INSERT INTO meta_polls (mode, query_id, trigger, started_at, status, cursor_before) "
                     "VALUES (?, ?, ?, ?, 'running', ?)",
                     (mode, query, trigger, iso(now), json.dumps(cursor_before) if cursor_before else None))
    return int(c.lastrowid or 0)


def finish_poll(conn: sqlite3.Connection, poll_id: int, now: float, *, status: str, reason: str | None = None,
                requests: int = 0, usage: Any = None, cursor_after: Any = None) -> None:
    conn.execute("UPDATE meta_polls SET finished_at=?, status=?, reason=?, requests=requests+?, usage=COALESCE(?, "
                 "usage), cursor_after=? WHERE id=?",
                 (iso(now), status, reason, requests, json.dumps(usage) if usage else None,
                  json.dumps(cursor_after) if cursor_after else None, poll_id))
    r = conn.execute("SELECT mode, query_id, trigger, requests, pages, records, new_ads, new_revisions, status "
                     "FROM meta_polls WHERE id=?", (poll_id,)).fetchone()
    if r is not None:
        events.append(conn, EVENT_POLL, {"poll_id": poll_id, "mode": r[0], "query_id": r[1], "trigger": r[2],
                                         "requests": r[3], "pages": r[4], "records": r[5], "new_ads": r[6],
                                         "new_revisions": r[7], "status": r[8]})


def base_context(conn: sqlite3.Connection) -> str:
    """Correlation context of a new analysis: the latest analyse run (the base the open-set decision uses)."""
    try:
        r = conn.execute("SELECT id FROM runs WHERE kind='analyse' ORDER BY created_at DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return "none"
    return r[0] if r else "none"


def materials_policy(conn: sqlite3.Connection) -> str:
    """Part of the idempotency key: switching snapshot/media download on is a new analysis of the same revision."""
    import os

    st = get_state(conn, "config", {}) or {}
    on = bool(st.get("media_fetch")) or os.environ.get("HYDRA_META_SNAPSHOTS", "").strip() == "1"
    return f"m{int(on)}l{int(bool(st.get('landing_fetch')))}"


def idem_key(mode: str, ad_id: str, content_sha: str, context: str, policy: str = "m0l0") -> str:
    return hashlib.sha256(f"{mode}|{ad_id}|{content_sha}|{EXTRACT_VERSION}|{context}|{policy}".encode()).hexdigest()


def commit_page(conn: sqlite3.Connection, *, poll_id: int, mode: str, query_id_: str | None, config_version: int,
                page: int, normalized: list[dict[str, Any]], now: float, enqueue: bool = True,
                qid_cursor: tuple[str, dict[str, Any] | None, str] | None = None) -> dict[str, Any]:
    """One fetched page in ONE transaction: observations, revisions, query provenance, outbox rows and (last) the
    query cursor. A crash before COMMIT leaves nothing (the page is read again); a page read twice adds sightings
    but no revision, no outbox row and no incident (UNIQUE keys)."""
    ts = iso(now)
    ctx = base_context(conn)
    new_ads = new_revs = 0
    rev_ids: list[int] = []
    queued: list[int] = []
    for n in normalized:
        cr, meta = n["creative"], n["meta"]
        ad_id = cr["source_ad_id"]
        if not ad_id:
            continue
        sha = normalize.content_sha256(cr)
        row = conn.execute("SELECT seen_count FROM meta_ads WHERE mode=? AND ad_id=?", (mode, ad_id)).fetchone()
        if row is None:
            conn.execute("INSERT INTO meta_ads (mode, ad_id, page_id, page_name, first_observed_at, last_observed_at,"
                         " seen_count, revisions) VALUES (?, ?, ?, ?, ?, ?, 1, 0)",
                         (mode, ad_id, cr.get("source_page_id"), cr.get("source_page_name"), ts, ts))
            new_ads += 1
        else:
            conn.execute("UPDATE meta_ads SET last_observed_at=?, seen_count=seen_count+1, page_name=COALESCE(?, "
                         "page_name) WHERE mode=? AND ad_id=?", (ts, cr.get("source_page_name"), mode, ad_id))
        rv = conn.execute("SELECT id FROM meta_revisions WHERE mode=? AND ad_id=? AND content_sha256=?",
                          (mode, ad_id, sha)).fetchone()
        if rv is None:
            nrev = conn.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM meta_revisions WHERE mode=? AND ad_id=?",
                                (mode, ad_id)).fetchone()[0]
            c = conn.execute("INSERT INTO meta_revisions (mode, ad_id, rev, content_sha256, content, graph_api_version,"
                             " countries, observed_at, collection_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                             "'metadata_available')",
                             (mode, ad_id, nrev, sha, json.dumps(n, ensure_ascii=False), meta.get("graph_api_version"),
                              json.dumps(meta.get("countries_requested") or []), ts))
            rid = int(c.lastrowid or 0)
            conn.execute("UPDATE meta_ads SET current_rev_id=?, revisions=revisions+1 WHERE mode=? AND ad_id=?",
                         (rid, mode, ad_id))
            new_revs += 1
            rev_ids.append(rid)
            if enqueue:
                oid = enqueue_analysis(conn, mode=mode, ad_id=ad_id, revision_id=rid, content_sha=sha, context=ctx,
                                       now=now)
                if oid:
                    queued.append(oid)
        else:
            rid = int(rv[0])
            # metadata only (delivery dates, platforms, EU values): updated in place, no revision, no extraction
            old = json.loads(conn.execute("SELECT content FROM meta_revisions WHERE id=?", (rid,)).fetchone()[0])
            if old.get("meta") != meta:
                old["meta"] = meta
                conn.execute("UPDATE meta_revisions SET content=? WHERE id=?", (json.dumps(old, ensure_ascii=False),
                                                                                rid))
            # CR-ASTRA-SW-07: a return to an already known content (A -> B -> A) makes it current again. Content
            # dedup (no third copy of A, history A/B kept) is separate from the chronology of observations.
            cur = conn.execute("SELECT current_rev_id FROM meta_ads WHERE mode=? AND ad_id=?",
                               (mode, ad_id)).fetchone()
            if cur is not None and cur[0] != rid and not _attachment_of(conn, cur[0], cr):
                conn.execute("UPDATE meta_ads SET current_rev_id=? WHERE mode=? AND ad_id=?", (rid, mode, ad_id))
        if query_id_:
            conn.execute("INSERT INTO meta_ad_queries (mode, ad_id, query_id, config_version, first_at, last_at, hits)"
                         " VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(mode, ad_id, query_id) DO UPDATE SET "
                         "last_at=excluded.last_at, hits=hits+1, config_version=excluded.config_version",
                         (mode, ad_id, query_id_, config_version, ts, ts))
        conn.execute("INSERT INTO meta_observations (poll_id, mode, ad_id, query_id, revision_id, page, observed_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (poll_id, mode, ad_id, query_id_, rid, page, ts))
    conn.execute("UPDATE meta_polls SET pages=pages+1, records=records+?, new_ads=new_ads+?, "
                 "new_revisions=new_revisions+? WHERE id=?", (len(normalized), new_ads, new_revs, poll_id))
    if qid_cursor is not None:  # cursor moves only together with the stored page (same transaction)
        qid, cursor, status = qid_cursor
        set_query_cursor(conn, qid, mode, cursor, now, status=status, ok=cursor is None)
    return {"records": len(normalized), "new_ads": new_ads, "new_revisions": new_revs, "revision_ids": rev_ids,
            "outbox_ids": queued}


ATTACH_REASON = "Materiał dołączony przez analityka."


def _attachment_of(conn: sqlite3.Connection, current_rev_id: int | None, observed: dict[str, Any]) -> bool:
    """True when the current revision is an analyst attachment on top of exactly the observed source content: a
    repeated sighting of that content must not silently drop the analyst's material from the current view."""
    if current_rev_id is None:
        return False
    r = conn.execute("SELECT content, collection_reason FROM meta_revisions WHERE id=?", (current_rev_id,)).fetchone()
    if r is None or r[1] != ATTACH_REASON:
        return False
    cur = dict(json.loads(r[0])["creative"])
    added = set(cur.get("media_digests") or []) - set(observed.get("media_digests") or [])
    cur["media_digests"] = sorted(set(cur.get("media_digests") or []) - added)
    base = dict(observed)
    base["media_digests"] = sorted(set(observed.get("media_digests") or []))
    return bool(added) and normalize.content_sha256(cur) == normalize.content_sha256(base)


# ------------------------------------------------------------------------------------------------ outbox
def enqueue_analysis(conn: sqlite3.Connection, *, mode: str, ad_id: str, revision_id: int, content_sha: str,
                     context: str, now: float) -> int | None:
    key = idem_key(mode, ad_id, content_sha, context, materials_policy(conn))
    c = conn.execute("INSERT OR IGNORE INTO meta_outbox (idem_key, mode, ad_id, revision_id, status, created_at, "
                     "updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                     (key, mode, ad_id, revision_id, iso(now), iso(now)))
    return int(c.lastrowid) if c.rowcount else None


def request_analysis(conn: sqlite3.Connection, mode: str, ad_id: str, now: float) -> tuple[dict[str, Any], bool]:
    """Analyst "analyse again" / resume: idempotent for the same revision and context; a failed or deferred job is
    re-queued instead of duplicated."""
    r = conn.execute("SELECT a.current_rev_id, r.content_sha256 FROM meta_ads a JOIN meta_revisions r ON "
                     "r.id=a.current_rev_id WHERE a.mode=? AND a.ad_id=?", (mode, ad_id)).fetchone()
    if r is None:
        raise KeyError(ad_id)
    rid, sha = int(r[0]), r[1]
    key = idem_key(mode, ad_id, sha, base_context(conn), materials_policy(conn))
    created = enqueue_analysis(conn, mode=mode, ad_id=ad_id, revision_id=rid, content_sha=sha,
                               context=base_context(conn), now=now) is not None
    if not created:
        conn.execute("UPDATE meta_outbox SET status='queued', not_before=NULL, updated_at=? WHERE idem_key=? AND "
                     "status IN ('failed', 'deferred', 'superseded')", (iso(now), key))
    return outbox_row(conn, key), created


OCOLS = ("id", "idem_key", "mode", "ad_id", "revision_id", "status", "attempts", "owner", "lease_until",
         "not_before", "last_error", "analysis_id", "created_at", "updated_at")


def _orow(r: Any) -> dict[str, Any]:
    d = dict(zip(OCOLS, r, strict=True))
    d["status_pl"] = OUTBOX_PL.get(d["status"], d["status"])
    d["last_error_pl"] = d.pop("last_error")
    return d


def outbox_row(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    return _orow(conn.execute(f"SELECT {', '.join(OCOLS)} FROM meta_outbox WHERE idem_key=?", (key,)).fetchone())


def claim(conn: sqlite3.Connection, owner: str, now: float, lease_s: float = 900.0) -> dict[str, Any] | None:
    """Lease the oldest runnable job (queued, deferred past not_before, or running with an expired lease after a
    crash/restart). One owner at a time per job; the lease is renewed by finishing."""
    ts = iso(now)
    ctx, policy = base_context(conn), materials_policy(conn)
    while True:
        r = conn.execute(f"SELECT {', '.join(OCOLS)} FROM meta_outbox WHERE (status='queued' AND (not_before IS NULL "
                         "OR not_before<=?)) OR (status='deferred' AND not_before<=?) OR (status='running' AND "
                         "lease_until<?) ORDER BY id LIMIT 1", (ts, ts, ts)).fetchone()
        if r is None:
            return None
        job = _orow(r)
        # CR-ASTRA-SW-08: the key names the context that is actually executed. The context (base run, materials
        # policy) may have changed since enqueue, so the job is re-keyed to it here, in the same transaction as the
        # lease; the worker then runs exactly job["context"]. If an equivalent job for that context already exists,
        # this one is superseded instead of doing the same work twice.
        sha = conn.execute("SELECT content_sha256 FROM meta_revisions WHERE id=?", (job["revision_id"],)).fetchone()
        key = idem_key(job["mode"], job["ad_id"], sha[0], ctx, policy) if sha else job["idem_key"]
        if key != job["idem_key"]:
            other = conn.execute("SELECT id, analysis_id FROM meta_outbox WHERE idem_key=?", (key,)).fetchone()
            if other is not None:
                conn.execute("UPDATE meta_outbox SET status='superseded', lease_until=NULL, analysis_id=?, "
                             "last_error=?, updated_at=? WHERE id=?",
                             (other[1], f"Zastąpione równoważnym zadaniem #{other[0]} (ten sam kontekst).", ts,
                              job["id"]))
                continue
            conn.execute("UPDATE meta_outbox SET idem_key=? WHERE id=?", (key, job["id"]))
            job["idem_key"] = key
        conn.execute("UPDATE meta_outbox SET status='running', owner=?, lease_until=?, attempts=attempts+1, "
                     "updated_at=? WHERE id=?", (owner, iso(now + lease_s), ts, job["id"]))
        job.update(status="running", owner=owner, attempts=job["attempts"] + 1, context=ctx, policy=policy)
        return job


def finish_job(conn: sqlite3.Connection, job_id: int, owner: str, now: float, *, status: str,
               error: str | None = None, analysis_id: int | None = None, not_before: float | None = None) -> bool:
    r = conn.execute("UPDATE meta_outbox SET status=?, last_error=?, analysis_id=COALESCE(?, analysis_id), "
                     "not_before=?, lease_until=NULL, updated_at=? WHERE id=? AND owner=?",
                     (status, error, analysis_id, iso(not_before) if not_before else None, iso(now), job_id, owner))
    return bool(r.rowcount)


def analyses_last_hour(conn: sqlite3.Connection, now: float, mode: str | None = None) -> int:
    """Analyses stored in the last hour, per data space (a replay never uses up the live cap)."""
    sql = "SELECT COUNT(*) FROM meta_analyses WHERE created_at>=?" + (" AND mode=?" if mode else "")
    return int(conn.execute(sql, (iso(now - 3600), mode) if mode else (iso(now - 3600),)).fetchone()[0])


# ------------------------------------------------------------------------------------------------ artifacts
def put_artifact(conn: sqlite3.Connection, revision_id: int, *, role: str, slot: str, status: str, source: str,
                 now: float, reason: str | None = None, artifact_id: str | None = None, url_redacted: str | None = None,
                 mime: str | None = None, size: int | None = None, derived: bool = False) -> None:
    conn.execute("INSERT INTO meta_artifacts (revision_id, role, slot, status, reason, artifact_id, source, "
                 "url_redacted, mime, size, derived, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(revision_id, role, slot, source) DO UPDATE SET status=excluded.status, "
                 "reason=excluded.reason, artifact_id=excluded.artifact_id, url_redacted=excluded.url_redacted, "
                 "mime=excluded.mime, size=excluded.size, derived=excluded.derived, fetched_at=excluded.fetched_at",
                 (revision_id, role, slot, status, reason, artifact_id, source, url_redacted, mime, size, int(derived),
                  iso(now)))


ACOLS = ("role", "slot", "status", "reason", "artifact_id", "source", "url_redacted", "mime", "size", "derived",
         "fetched_at")
RETRYABLE = {"budget_deferred", "asset_discovery_pending", "blocked_or_login_required"}


def artifacts(conn: sqlite3.Connection, revision_id: int) -> list[dict[str, Any]]:
    out = []
    for r in conn.execute(f"SELECT {', '.join(ACOLS)} FROM meta_artifacts WHERE revision_id=? ORDER BY id",
                          (revision_id,)):
        d = dict(zip(ACOLS, r, strict=True))
        d["sha256"] = d.pop("artifact_id")
        d["derived"] = bool(d["derived"])
        d["status_pl"] = (d["reason"] or COLLECTION_PL.get(d["status"], d["status"]))
        d["retry_possible"] = d["status"] in RETRYABLE
        out.append(d)
    return out


def media_bytes(conn: sqlite3.Connection) -> int:
    """Bytes of downloaded Meta media (snapshot / CDN sources; analyst uploads and fixtures not counted)."""
    return int(conn.execute("SELECT COALESCE(SUM(size), 0) FROM (SELECT DISTINCT artifact_id, size FROM meta_artifacts "
                            "WHERE status='asset_available' AND source='snapshot_media')").fetchone()[0] or 0)


def set_collection(conn: sqlite3.Connection, revision_id: int, status: str, reason: str | None) -> None:
    conn.execute("UPDATE meta_revisions SET collection_status=?, collection_reason=? WHERE id=?",
                 (status, reason, revision_id))


def attach_revision(conn: sqlite3.Connection, mode: str, ad_id: str, digest: str, now: float) -> tuple[int, bool]:
    """Analyst attached a file: a new revision = current content + the attachment digest (hash changes)."""
    r = conn.execute("SELECT r.content FROM meta_ads a JOIN meta_revisions r ON r.id=a.current_rev_id "
                     "WHERE a.mode=? AND a.ad_id=?", (mode, ad_id)).fetchone()
    if r is None:
        raise KeyError(ad_id)
    content = json.loads(r[0])
    cr = dict(content["creative"])
    cr["media_digests"] = sorted(set(cr.get("media_digests") or []) | {digest})
    sha = normalize.content_sha256(cr)
    ex = conn.execute("SELECT id FROM meta_revisions WHERE mode=? AND ad_id=? AND content_sha256=?",
                      (mode, ad_id, sha)).fetchone()
    if ex is not None:
        return int(ex[0]), False
    nrev = conn.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM meta_revisions WHERE mode=? AND ad_id=?",
                        (mode, ad_id)).fetchone()[0]
    new = {"creative": cr, "meta": content["meta"]}
    c = conn.execute("INSERT INTO meta_revisions (mode, ad_id, rev, content_sha256, content, graph_api_version, "
                     "countries, observed_at, collection_status, collection_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                     "'asset_available', ?)",
                     (mode, ad_id, nrev, sha, json.dumps(new, ensure_ascii=False), content["meta"].get(
                         "graph_api_version"), json.dumps(content["meta"].get("countries_requested") or []), iso(now),
                      ATTACH_REASON))
    rid = int(c.lastrowid or 0)
    conn.execute("UPDATE meta_ads SET current_rev_id=?, revisions=revisions+1 WHERE mode=? AND ad_id=?",
                 (rid, mode, ad_id))
    events.append(conn, EVENT_ATTACH, {"mode": mode, "ad_id": ad_id, "revision_id": rid, "sha256": digest})
    return rid, True


# ------------------------------------------------------------------------------------------------ analyses
def save_analysis(conn: sqlite3.Connection, *, key: str, mode: str, ad_id: str, revision_id: int, now: float,
                  **f: Any) -> int:
    cols = ("incident_id", "base_run_id", "context_version", "assign_run_id", "status", "reason", "decision",
            "hydra_campaign_id", "correlation", "triage", "manipulation", "triage_priority", "report_id")
    vals = {k: f.get(k) for k in cols}
    for k in ("correlation", "triage", "manipulation"):
        vals[k] = json.dumps(vals[k] if vals[k] is not None else ({} if k != "manipulation" else []),
                             ensure_ascii=False, default=str)
    vals["triage_priority"] = vals["triage_priority"] or "unknown"
    vals["status"] = vals["status"] or "failed"
    conn.execute(f"INSERT INTO meta_analyses (idem_key, mode, ad_id, revision_id, {', '.join(cols)}, created_at, "
                 f"updated_at) VALUES (?, ?, ?, ?, {', '.join('?' * len(cols))}, ?, ?) ON CONFLICT(idem_key) DO "
                 f"UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}, updated_at=excluded.updated_at",
                 (key, mode, ad_id, revision_id, *[vals[c] for c in cols], iso(now), iso(now)))
    aid = int(conn.execute("SELECT id FROM meta_analyses WHERE idem_key=?", (key,)).fetchone()[0])
    events.append(conn, EVENT_ANALYSIS, {"analysis_id": aid, "mode": mode, "ad_id": ad_id, "revision_id": revision_id,
                                         "status": vals["status"], "decision": vals["decision"],
                                         "incident_id": vals["incident_id"]})
    return aid


REVIEW_DECISIONS = ("confirmed_by_analyst", "dismissed_by_analyst", "unreviewed")


def review(conn: sqlite3.Connection, analysis_id: int, decision: str, analyst: str, note: str | None,
           now: float) -> bool:
    if decision not in REVIEW_DECISIONS:
        raise ValueError("Decyzja: confirmed_by_analyst, dismissed_by_analyst albo unreviewed.")
    # CR-ASTRA-SW-09: a review is not a new analysis; updated_at (reported as analysed_at, and the period report
    # window) changes only when the analysis is stored. The review time lives in meta_reviews (history).
    r = conn.execute("UPDATE meta_analyses SET review_status=? WHERE id=?", (decision, analysis_id))
    if not r.rowcount:
        return False
    conn.execute("INSERT INTO meta_reviews (analysis_id, decision, analyst, note, created_at) VALUES (?, ?, ?, ?, ?)",
                 (analysis_id, decision, analyst[:100], (note or "")[:1000] or None, iso(now)))
    events.append(conn, EVENT_REVIEW, {"analysis_id": analysis_id, "decision": decision, "analyst": analyst[:100]})
    return True


ANCOLS = ("id", "idem_key", "mode", "ad_id", "revision_id", "incident_id", "base_run_id", "context_version",
          "assign_run_id", "status", "reason", "decision", "hydra_campaign_id", "correlation", "triage", "manipulation",
          "triage_priority", "review_status", "report_id", "created_at", "updated_at")
ANALYSIS_PL = {"done": "analiza zakończona", "partial": "analiza częściowa", "failed": "analiza nieudana",
               "running": "w toku", "queued": "w kolejce"}
PRIORITY_PL = {"unknown": "brak oceny", "low": "niski", "medium": "średni", "high": "wysoki"}
DECISION_PL = {"assign": "Przypisano do znanej kampanii", "new_campaign": "Nowy wzorzec (brak znanej kampanii)",
               "abstain": "Abstencja: dowody niewystarczające", None: "Brak decyzji"}


def alert_levels(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Triage priorities that raise an alert (config rules.alert_min_priority, default medium)."""
    lvl = ((get_state(conn, "config", {}) or {}).get("rules") or {}).get("alert_min_priority") or "medium"
    return ALERT_LEVELS.get(lvl, ALERT_LEVELS["medium"])


def _levels_sql(levels: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{x}'" for x in levels if x in PRIORITY_PL) + ")"


def is_alert(a: dict[str, Any], levels: tuple[str, ...] = ALERT_LEVELS["medium"]) -> bool:
    return a.get("review_status") == "unreviewed" and (a.get("triage_priority") in levels
                                                       or a.get("decision") == "assign")


def _arow(r: Any, levels: tuple[str, ...] = ALERT_LEVELS["medium"]) -> dict[str, Any]:
    d = dict(zip(ANCOLS, r, strict=True))
    for k in ("correlation", "triage", "manipulation"):
        d[k] = json.loads(d[k] or ("[]" if k == "manipulation" else "{}"))
    d["status_pl"] = ANALYSIS_PL.get(d["status"], d["status"])
    d["mode_label_pl"] = MODE_PL.get(d["mode"], d["mode"])
    d["reason_pl"] = d.pop("reason")
    d["alert"] = is_alert(d, levels)
    return d


def analysis(conn: sqlite3.Connection, aid: int) -> dict[str, Any] | None:
    r = conn.execute(f"SELECT {', '.join(ANCOLS)} FROM meta_analyses WHERE id=?", (aid,)).fetchone()
    if r is None:
        return None
    a = _arow(r, alert_levels(conn))
    a["review"] = {"status": a["review_status"], "history": [
        {"decision": x[0], "analyst": x[1], "note": x[2], "created_at": x[3]} for x in conn.execute(
            "SELECT decision, analyst, note, created_at FROM meta_reviews WHERE analysis_id=? ORDER BY id", (aid,))]}
    a["triage"]["review_status"] = a["review_status"]
    return a


def analyses_for(conn: sqlite3.Connection, mode: str, ad_id: str) -> list[dict[str, Any]]:
    lv = alert_levels(conn)
    return [_arow(r, lv) for r in conn.execute(f"SELECT {', '.join(ANCOLS)} FROM meta_analyses WHERE mode=? AND "
                                               "ad_id=? ORDER BY id DESC", (mode, ad_id))]


def _summary(a: dict[str, Any] | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {"id": a["id"], "status": a["status"], "status_pl": a["status_pl"], "decision": a["decision"],
            "decision_pl": DECISION_PL.get(a["decision"], a["decision"]), "campaign_id": a["hydra_campaign_id"],
            "triage_priority": a["triage_priority"], "triage_priority_pl": PRIORITY_PL.get(a["triage_priority"]),
            "review_status": a["review_status"], "coverage_pl": (a["correlation"] or {}).get("coverage_pl")
            or a.get("reason_pl")}


# ------------------------------------------------------------------------------------------------ readers
def _rev(conn: sqlite3.Connection, rid: int | None) -> dict[str, Any] | None:
    if rid is None:
        return None
    r = conn.execute("SELECT id, rev, content_sha256, observed_at, collection_status, collection_reason, content, "
                     "graph_api_version FROM meta_revisions WHERE id=?", (rid,)).fetchone()
    if r is None:
        return None
    return {"id": r[0], "rev": r[1], "content_sha256": r[2], "observed_at": r[3], "collection_status": r[4],
            "collection_status_pl": COLLECTION_PL.get(r[4], r[4]), "collection_reason_pl": r[5],
            "content": json.loads(r[6]), "graph_api_version": r[7]}


def ad_row(conn: sqlite3.Connection, r: Any) -> dict[str, Any]:
    mode, ad_id, page_id, page_name, first, last, seen, cur, nrev = r
    rev = _rev(conn, cur)
    content = (rev or {}).pop("content", None) or {"creative": {}, "meta": {}}
    cr, meta = content.get("creative") or {}, content.get("meta") or {}
    from hydra.watch.rt import indicators as ind

    last_a = None
    if cur is not None:
        x = conn.execute(f"SELECT {', '.join(ANCOLS)} FROM meta_analyses WHERE revision_id=? ORDER BY id DESC "
                         "LIMIT 1", (cur,)).fetchone()
        last_a = _arow(x, alert_levels(conn)) if x else None
    qs = [q[0] or q[1] for q in conn.execute(
        "SELECT q.label, q.value FROM meta_ad_queries aq LEFT JOIN meta_queries q ON q.id=aq.query_id WHERE "
        "aq.mode=? AND aq.ad_id=? ORDER BY aq.first_at", (mode, ad_id))]
    return {"ad_id": ad_id, "mode": mode, "mode_label_pl": MODE_PL.get(mode, mode), "page_id": page_id,
            "page_name": page_name, "first_observed_at": first, "last_observed_at": last, "seen_count": seen,
            "revisions": nrev, "current_revision": rev, "text_excerpt": normalize.text_of(cr)[:300],
            "link_hosts": [ind.display("host", h) for h in normalize.hosts(cr, meta)][:10], "queries": qs,
            "analysis": _summary(last_a), "alert": bool(last_a and last_a["alert"])}


ADCOLS = "a.mode, a.ad_id, a.page_id, a.page_name, a.first_observed_at, a.last_observed_at, a.seen_count, " \
         "a.current_rev_id, a.revisions"


def list_ads(conn: sqlite3.Connection, mode: str, *, view: str = "all", q: str | None = None,
             before: str | None = None, limit: int = 50) -> dict[str, Any]:
    where, args = ["a.mode=?"], [mode]
    if view == "unprocessed":
        where.append("NOT EXISTS (SELECT 1 FROM meta_analyses m WHERE m.revision_id=a.current_rev_id AND m.status IN "
                     "('done', 'partial'))")
    elif view == "new_revisions":
        where.append("a.revisions>1")
    elif view == "alerts":
        where.append("EXISTS (SELECT 1 FROM meta_analyses m WHERE m.revision_id=a.current_rev_id AND "
                     f"m.review_status='unreviewed' AND (m.triage_priority IN {_levels_sql(alert_levels(conn))} OR "
                     "m.decision='assign'))")
    if q:
        where.append("(a.ad_id LIKE ? OR a.page_name LIKE ? OR EXISTS (SELECT 1 FROM meta_revisions r WHERE "
                     "r.id=a.current_rev_id AND r.content LIKE ?))")
        args += [f"%{q}%"] * 3
    if before:
        where.append("(a.last_observed_at || '|' || a.ad_id) < ?")
        args.append(before)
    rows = conn.execute(f"SELECT {ADCOLS} FROM meta_ads a WHERE {' AND '.join(where)} ORDER BY a.last_observed_at "
                        "DESC, a.ad_id DESC LIMIT ?", (*args, limit + 1)).fetchall()
    ads = [ad_row(conn, r) for r in rows[:limit]]
    nxt = f"{ads[-1]['last_observed_at']}|{ads[-1]['ad_id']}" if len(rows) > limit and ads else None
    return {"mode": mode, "mode_label_pl": MODE_PL.get(mode, mode), "ads": ads, "next_before": nxt}


def ad_detail(conn: sqlite3.Connection, mode: str, ad_id: str) -> dict[str, Any] | None:
    r = conn.execute(f"SELECT {ADCOLS} FROM meta_ads a WHERE a.mode=? AND a.ad_id=?", (mode, ad_id)).fetchone()
    if r is None:
        return None
    revs = []
    for (rid,) in conn.execute("SELECT id FROM meta_revisions WHERE mode=? AND ad_id=? ORDER BY rev DESC",
                               (mode, ad_id)):
        rv = _rev(conn, rid)
        assert rv is not None
        rv["artifacts"] = artifacts(conn, rid)
        revs.append(rv)
    qs = [{"query_id": x[0], "label": x[1] or x[2], "kind": x[3], "origin": x[4], "first_at": x[5], "last_at": x[6],
           "hits": x[7], "config_version": x[8]} for x in conn.execute(
        "SELECT aq.query_id, q.label, q.value, q.kind, q.origin, aq.first_at, aq.last_at, aq.hits, aq.config_version "
        "FROM meta_ad_queries aq LEFT JOIN meta_queries q ON q.id=aq.query_id WHERE aq.mode=? AND aq.ad_id=? "
        "ORDER BY aq.first_at", (mode, ad_id))]
    from hydra.meta.contract import library_link

    return {"ad": ad_row(conn, r), "source_link": library_link(ad_id), "revisions": revs, "queries": qs,
            "analyses": [_summary(a) for a in analyses_for(conn, mode, ad_id)]}


def queue(conn: sqlite3.Connection, mode: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = (f"SELECT {', '.join(OCOLS)} FROM meta_outbox" + (" WHERE mode=?" if mode else "")
           + " ORDER BY id DESC LIMIT ?")
    return [_orow(r) for r in conn.execute(sql, (mode, limit) if mode else (limit,))]


def queue_counts(conn: sqlite3.Connection, mode: str | None = None) -> dict[str, int]:
    out = {k: 0 for k in OUTBOX_PL}
    sql = "SELECT status, COUNT(*) FROM meta_outbox" + (" WHERE mode=?" if mode else "") + " GROUP BY status"
    for s, n in conn.execute(sql, (mode,) if mode else ()):
        out[s] = n
    return out


def counters(conn: sqlite3.Connection, mode: str) -> dict[str, int]:
    """Separate counters, never summed into one number (spec §9)."""
    one = lambda sql, *a: int(conn.execute(sql, (mode, *a)).fetchone()[0] or 0)  # noqa: E731
    return {
        "records_fetched": one("SELECT COALESCE(SUM(records), 0) FROM meta_polls WHERE mode=?"),
        "unique_ads": one("SELECT COUNT(*) FROM meta_ads WHERE mode=?"),
        "new_revisions": one("SELECT COUNT(*) FROM meta_revisions WHERE mode=? AND rev>1"),
        "assets_acquired": one("SELECT COUNT(DISTINCT x.artifact_id) FROM meta_artifacts x JOIN meta_revisions r ON "
                               "r.id=x.revision_id WHERE r.mode=? AND x.status='asset_available'"),
        "analyses_done": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND status='done'"),
        "analyses_partial": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND status='partial'"),
        "assigned": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND decision='assign'"),
        "new_patterns": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND decision='new_campaign'"),
        "abstained": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND decision='abstain'"),
        "alerts_open": one("SELECT COUNT(*) FROM meta_analyses m JOIN meta_ads a ON a.mode=m.mode AND "
                           "a.current_rev_id=m.revision_id WHERE m.mode=? AND m.review_status='unreviewed' AND "
                           f"(m.triage_priority IN {_levels_sql(alert_levels(conn))} OR m.decision='assign')"),
        "analyst_confirmed": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND "
                                 "review_status='confirmed_by_analyst'"),
        "analyst_dismissed": one("SELECT COUNT(*) FROM meta_analyses WHERE mode=? AND "
                                 "review_status='dismissed_by_analyst'"),
    }


def last_polls(conn: sqlite3.Connection, mode: str, limit: int = 20) -> list[dict[str, Any]]:
    cols = ("id", "query_id", "trigger", "started_at", "finished_at", "requests", "pages", "records", "new_ads",
            "new_revisions", "status", "reason")
    return [dict(zip(cols, r, strict=True)) for r in conn.execute(
        f"SELECT {', '.join(cols)} FROM meta_polls WHERE mode=? ORDER BY id DESC LIMIT ?", (mode, limit))]


def retention(conn: sqlite3.Connection, now: float, *, observation_days: int = 30, poll_days: int = 30
              ) -> dict[str, int]:
    """Sightings and poll rows age out; ads, revisions, artifacts relations and analyses (evidence) are kept."""
    a = conn.execute("DELETE FROM meta_observations WHERE observed_at < ?", (iso(now - observation_days * 86400),))
    b = conn.execute("DELETE FROM meta_polls WHERE started_at < ? AND status != 'running'",
                     (iso(now - poll_days * 86400),))
    return {"observations": a.rowcount, "polls": b.rowcount}
