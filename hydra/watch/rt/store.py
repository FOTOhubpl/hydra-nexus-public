"""WATCH-RT persistence (schema v6, hydra/db.py). Writer-side functions take the Writer's connection (they run inside
`Writer.run`); reader functions take a read-only connection. No other write connection is ever opened."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from hydra import events
from hydra.watch.rt import indicators as ind
from hydra.watch.rt.sources import ADAPTERS, RawItem
from hydra.watch.rt.watchlist import Entry, rarity_bits, strength

EVENT_SOURCE = "WATCH_SOURCE_CHANGED"
EVENT_WATCHLIST = "WATCH_WATCHLIST_CHANGED"
EVENT_MATCH = "WATCH_MATCH_STATUS"
EVENT_RESYNC = "WATCH_SOURCE_RESYNC"
REPLAY_EXT_PREFIX = "replay:"  # watch_items.external_id of replay rows (see commit_poll)
SOURCE_COLS = ("id", "enabled", "interval_s", "state", "state_detail", "last_poll", "last_ok", "next_due", "etag",
               "last_modified", "cursor", "consecutive_errors", "circuit_open_until", "total_polls", "total_items",
               "last_latency_ms", "last_error", "config", "updated_at")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------ schema
def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Migration v6 statements (all IF NOT EXISTS) when the tables are missing: a DB whose user_version passed 6
    through another branch's migration before v6 was merged. Runs on the Writer connection. True = created."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watch_sources'").fetchone():
        return False
    from hydra.db import MIGRATIONS, _split

    sql = dict(MIGRATIONS)[6]
    for statement in _split(sql):
        conn.execute(statement)
    return True


# ------------------------------------------------------------------------------------------------ sources
def ensure_sources(conn: sqlite3.Connection, now: float) -> None:
    for a in ADAPTERS.values():
        conn.execute("INSERT OR IGNORE INTO watch_sources (id, enabled, interval_s, state, updated_at) "
                     "VALUES (?, 0, ?, 'disabled', ?)", (a.id, a.default_interval_s, iso(now)))


def sources(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out = {}
    for r in conn.execute(f"SELECT {', '.join(SOURCE_COLS)} FROM watch_sources"):
        d = dict(zip(SOURCE_COLS, r, strict=True))
        d["cursor"] = json.loads(d["cursor"] or "{}")
        d["config"] = json.loads(d["config"] or "{}")
        out[d["id"]] = d
    return out


def update_source(conn: sqlite3.Connection, sid: str, now: float, **fields: Any) -> None:
    if "cursor" in fields:
        fields["cursor"] = json.dumps(fields["cursor"] or {}, ensure_ascii=False)
    if "config" in fields:
        fields["config"] = json.dumps(fields["config"] or {}, ensure_ascii=False)
    fields["updated_at"] = iso(now)
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE watch_sources SET {cols} WHERE id=?", (*fields.values(), sid))


def set_source_config(conn: sqlite3.Connection, sid: str, now: float, *, enabled: bool | None,
                      interval_s: int | None, actor: str = "api") -> None:
    f: dict[str, Any] = {}
    if enabled is not None:
        f["enabled"] = int(enabled)
        f["state"] = "idle" if enabled else "disabled"
        f["next_due"] = iso(now) if enabled else None
        f["consecutive_errors"] = 0
        f["circuit_open_until"] = None
    if interval_s is not None:
        f["interval_s"] = int(interval_s)
    if f:
        update_source(conn, sid, now, **f)
        events.append(conn, EVENT_SOURCE, {"source": sid, "enabled": enabled, "interval_s": interval_s,
                                           "actor": actor})


# ------------------------------------------------------------------------------------------------ poll commit
def commit_poll(conn: sqlite3.Connection, sid: str, *, started: float, duration_ms: int, items: list[RawItem],
                matches: dict[str, list[tuple[str, str, Entry]]], http_status: int | None, not_modified: bool,
                error: str | None, replay: bool, source_fields: dict[str, Any], now: float,
                detail: str | None = None) -> dict[str, Any]:
    """One transaction: run row, dedup + insert items, insert matches, update the source row. A successful poll
    without new items leaves the source in state `no_new` ("Brak nowych wpisów"); `detail` (e.g. a CERT resync) is
    kept in `state_detail` and audited as WATCH_SOURCE_RESYNC."""
    rep = int(replay)
    cur = conn.execute("INSERT INTO watch_runs (source, started_at, duration_ms, fetched, http_status, not_modified,"
                       " error, replay) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (sid, iso(started), duration_ms, len(items), http_status, int(not_modified), error, rep))
    run_id = int(cur.lastrowid or 0)
    new_items: list[dict[str, Any]] = []
    new_matches: list[dict[str, Any]] = []
    ts = iso(now)
    seen_in_poll: set[str] = set()
    for it in items:
        if it.external_id in seen_in_poll:
            continue
        seen_in_poll.add(it.external_id)
        # replay rows get their own key space (UNIQUE (source, external_id)): replaying a live recording never bumps
        # or shadows the live row of the same feed item, and a later live poll never lands on a replay row
        ext = REPLAY_EXT_PREFIX + it.external_id if rep else it.external_id
        row = conn.execute("SELECT id, payload_sha256 FROM watch_items WHERE source=? AND external_id=?",
                           (sid, ext)).fetchone()
        if row is not None:
            # CR-ASTRA-SW-05: a known item is not a new item, but it is still new information: a changed payload
            # refreshes value/indicators, and the item is re-matched against the current watchlist (a host added
            # after the first sighting must match the next time the feed repeats it). Matches dedupe on
            # UNIQUE (item_id, watch_id), so repeating the same poll never duplicates a match.
            item_id = int(row[0])
            if row[1] != it.payload_sha256:
                conn.execute("UPDATE watch_items SET last_seen=?, seen_count=seen_count+1, payload_sha256=?, kind=?, "
                             "value=?, indicators=? WHERE id=?",
                             (ts, it.payload_sha256, it.kind, it.value[:2000],
                              json.dumps(it.indicators, ensure_ascii=False), item_id))
            else:
                conn.execute("UPDATE watch_items SET last_seen=?, seen_count=seen_count+1 WHERE id=?", (ts, item_id))
            is_new = False
        else:
            c = conn.execute("INSERT INTO watch_items (source, external_id, payload_sha256, kind, value, indicators, "
                             "first_seen, last_seen, run_id, replay) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (sid, ext, it.payload_sha256, it.kind, it.value[:2000],
                              json.dumps(it.indicators, ensure_ascii=False), it.first_seen, ts, run_id, rep))
            item_id = int(c.lastrowid or 0)
            is_new = True
        hits = matches.get(it.external_id) or []
        n_hit = 0
        for kind, value, e in hits:
            st = strength(kind, e)
            evidence = {"item_value": it.value[:300], "indicator_kind": kind, "matched_value": value,
                        "watch_origin": e.origin, "watch_label": e.label, "incident_ids": e.incident_ids[:10],
                        "feed": {k: v for k, v in it.payload.items() if k in ("threat", "tags", "url_status",
                                                                               "issuer", "keyword", "page_name",
                                                                               "ActionTime")}}
            mc = conn.execute(
                "INSERT OR IGNORE INTO watch_matches (item_id, watch_id, source, indicator_kind, indicator_value, "
                "campaign_id, run_id, strength, bits, df, status, evidence, replay, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
                (item_id, e.id, sid, kind, value, e.campaign_id or None, e.run_id, st,
                 rarity_bits(e.df, e.n_incidents), e.df, json.dumps(evidence, ensure_ascii=False), rep, ts, ts))
            if mc.rowcount:
                n_hit += 1
                new_matches.append({"id": int(mc.lastrowid or 0), "item_id": item_id})
        if n_hit:
            conn.execute("UPDATE watch_items SET match_count=(SELECT COUNT(*) FROM watch_matches WHERE item_id=?) "
                         "WHERE id=?", (item_id, item_id))
        if is_new:
            new_items.append({"id": item_id})
    conn.execute("UPDATE watch_runs SET new=?, matched=? WHERE id=?", (len(new_items), len(new_matches), run_id))
    if detail:
        events.append(conn, EVENT_RESYNC, {"source": sid, "run_id": run_id, "detail": detail, "replay": replay})
    if source_fields:
        if not error and source_fields.get("state") == "ok" and not new_items:
            source_fields["state"] = "no_new"
        if detail:
            source_fields["state_detail"] = detail
        if not error:
            source_fields["total_items"] = conn.execute(
                "SELECT total_items FROM watch_sources WHERE id=?", (sid,)).fetchone()[0] + len(new_items)
        update_source(conn, sid, now, **source_fields)
    return {"run_id": run_id, "item_ids": [i["id"] for i in new_items], "match_ids": [m["id"] for m in new_matches]}


# ------------------------------------------------------------------------------------------------ watchlist
def replace_campaign_entries(conn: sqlite3.Connection, entries: list[dict[str, Any]], now: float) -> dict[str, int]:
    old = {(r[1], r[2], r[3]): r[0] for r in conn.execute(
        "SELECT id, kind, value, campaign_id FROM watch_watchlist WHERE origin='campaign'")}
    new_keys = set()
    added = 0
    for e in entries:
        k = (e["kind"], e["value"], e["campaign_id"])
        new_keys.add(k)
        if k in old:
            conn.execute("UPDATE watch_watchlist SET run_id=?, label=?, incident_ids=?, df=?, n_incidents=?, "
                         "display=? WHERE id=?", (e["run_id"], e.get("label"), json.dumps(e["incident_ids"]),
                                                  e.get("df"), e.get("n_incidents"), e["display"], old[k]))
            continue
        conn.execute("INSERT INTO watch_watchlist (kind, value, display, origin, campaign_id, run_id, label, "
                     "incident_ids, df, n_incidents, created_at) VALUES (?, ?, ?, 'campaign', ?, ?, ?, ?, ?, ?, ?)",
                     (e["kind"], e["value"], e["display"], e["campaign_id"], e["run_id"], e.get("label"),
                      json.dumps(e["incident_ids"]), e.get("df"), e.get("n_incidents"), iso(now)))
        added += 1
    removed = [old[k] for k in old if k not in new_keys]
    for i in removed:
        conn.execute("DELETE FROM watch_watchlist WHERE id=?", (i,))
    total = conn.execute("SELECT COUNT(*) FROM watch_watchlist").fetchone()[0]
    events.append(conn, EVENT_WATCHLIST, {"action": "rebuild", "added": added, "removed": len(removed)})
    return {"added": added, "removed": len(removed), "total": total}


def add_entry(conn: sqlite3.Connection, kind: str, value: str, label: str | None, origin: str, now: float,
              campaign_id: str = "") -> int:
    key = ind.normalise(kind, value)
    if not key:
        raise ValueError(f"Niepoprawna wartość dla rodzaju „{ind.KIND_PL.get(kind, kind)}”.")
    conn.execute("INSERT OR IGNORE INTO watch_watchlist (kind, value, display, origin, campaign_id, label, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (kind, key, ind.display(kind, key), origin, campaign_id, (label or "")[:200] or None, iso(now)))
    rid = conn.execute("SELECT id FROM watch_watchlist WHERE kind=? AND value=? AND origin=? AND campaign_id=?",
                       (kind, key, origin, campaign_id)).fetchone()[0]
    if origin == "manual":
        events.append(conn, EVENT_WATCHLIST, {"action": "add", "kind": kind, "id": rid})
    return int(rid)


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    r = conn.execute("DELETE FROM watch_watchlist WHERE id=? AND origin IN ('manual', 'replay')", (entry_id,))
    if r.rowcount:
        events.append(conn, EVENT_WATCHLIST, {"action": "delete", "id": entry_id})
    return bool(r.rowcount)


def clear_replay(conn: sqlite3.Connection) -> None:
    """A replay starts from a clean slate of replay rows (live rows are never touched)."""
    conn.execute("DELETE FROM watch_matches WHERE replay=1")
    conn.execute("DELETE FROM watch_items WHERE replay=1")
    conn.execute("DELETE FROM watch_runs WHERE replay=1")
    conn.execute("DELETE FROM watch_watchlist WHERE origin='replay'")


def set_match_status(conn: sqlite3.Connection, mid: int, status: str, now: float) -> bool:
    r = conn.execute("UPDATE watch_matches SET status=?, updated_at=? WHERE id=?", (status, iso(now), mid))
    if r.rowcount:
        events.append(conn, EVENT_MATCH, {"id": mid, "status": status})
    return bool(r.rowcount)


def set_match_assign(conn: sqlite3.Connection, mid: int, assign: dict[str, Any], now: float) -> None:
    conn.execute("UPDATE watch_matches SET assign=?, updated_at=? WHERE id=?",
                 (json.dumps(assign, ensure_ascii=False), iso(now), mid))


def retention(conn: sqlite3.Connection, now: float, *, item_days: int, run_days: int) -> dict[str, int]:
    """Items without matches older than item_days and runs older than run_days are deleted; matched items and
    their matches are kept (evidence)."""
    ci = iso(now - item_days * 86400)
    cr = iso(now - run_days * 86400)
    a = conn.execute("DELETE FROM watch_items WHERE last_seen < ? AND match_count = 0", (ci,)).rowcount
    b = conn.execute("DELETE FROM watch_runs WHERE started_at < ?", (cr,)).rowcount
    return {"items": a, "runs": b}


# ------------------------------------------------------------------------------------------------ readers
def item_dict(r: sqlite3.Row | tuple) -> dict[str, Any]:
    (iid, source, ext, kind, value, indicators, first, last, count, mcount, replay) = r
    ext = ext.removeprefix(REPLAY_EXT_PREFIX) if replay else ext
    pairs = json.loads(indicators or "[]")
    return {"id": iid, "source": source, "external_id": ext, "kind": kind,
            "value_defanged": ind.display("url" if kind == "url" else "host", value) if kind in ("url", "domain",
                                                                                                    "cert") else value,
            "first_seen": first, "last_seen": last, "seen_count": count, "match_count": mcount,
            "indicators": [{"kind": k, "value_display": ind.display(k, v)} for k, v in pairs][:30],
            "replay": bool(replay)}


ITEM_COLS = "id, source, external_id, kind, value, indicators, first_seen, last_seen, seen_count, match_count, replay"


def list_items(conn: sqlite3.Connection, *, source: str | None = None, kind: str | None = None,
               matched: bool | None = None, q: str | None = None, before_id: int | None = None, limit: int = 100,
               ids: list[int] | None = None) -> list[dict[str, Any]]:
    where, args = [], []
    if ids is not None:
        where.append(f"id IN ({','.join('?' * len(ids)) or 'NULL'})")
        args += ids
    if source:
        where.append("source=?")
        args.append(source)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if matched is True:
        where.append("match_count>0")
    elif matched is False:
        where.append("match_count=0")
    if q:
        where.append("(value LIKE ? OR indicators LIKE ?)")
        args += [f"%{q.lower()}%", f"%{q.lower()}%"]
    if before_id:
        where.append("id<?")
        args.append(before_id)
    sql = f"SELECT {ITEM_COLS} FROM watch_items" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY id DESC LIMIT ?"
    return [item_dict(r) for r in conn.execute(sql, (*args, limit))]


MATCH_COLS = ("m.id, m.item_id, m.source, m.indicator_kind, m.indicator_value, m.watch_id, w.origin, m.campaign_id, "
              "w.label, m.run_id, w.incident_ids, m.strength, m.bits, m.df, w.n_incidents, m.status, m.created_at, "
              "m.updated_at, m.evidence, m.assign, i.value, i.kind, i.first_seen, m.replay")


def _safe_evidence(ev: dict[str, Any], item_kind: str | None) -> dict[str, Any]:
    """Hosts and URLs in evidence are defanged for display (never clickable), hashes stay masked."""
    out = dict(ev)
    if out.get("item_value"):
        out["item_value"] = ind.display("url" if item_kind == "url" else "host", str(out["item_value"]))
    k = str(out.get("indicator_kind") or "")
    if out.get("matched_value") and k:
        out["matched_value"] = ind.display(k, str(out["matched_value"]))
    return out


def match_dict(r: tuple) -> dict[str, Any]:
    (mid, item_id, source, k, v, wid, origin, cid, label, run, incs, st, bits, df, n, status, created, updated,
     evidence, assign, ivalue, ikind, ifirst, replay) = r
    return {"id": mid, "item_id": item_id, "source": source, "indicator_kind": k,
            "indicator_kind_pl": ind.KIND_PL.get(k, k), "indicator_display": ind.display(k, v), "watch_id": wid,
            "origin": origin, "campaign_id": cid, "campaign_label": label, "run_id": run,
            "incident_ids": json.loads(incs or "[]"), "strength": st, "bits": bits, "df": df, "n_incidents": n,
            "status": status, "created_at": created, "updated_at": updated,
            "evidence": _safe_evidence(json.loads(evidence or "{}"), ikind),
            "assign": json.loads(assign) if assign else None,
            "item": {"value_defanged": ind.display("url" if ikind == "url" else "host", ivalue or ""),
                     "first_seen": ifirst, "kind": ikind},
            "replay": bool(replay)}


def list_matches(conn: sqlite3.Connection, *, status: str | None = None, limit: int = 100,
                 ids: list[int] | None = None, before_id: int | None = None) -> list[dict[str, Any]]:
    where, args = [], []
    if ids is not None:
        where.append(f"m.id IN ({','.join('?' * len(ids)) or 'NULL'})")
        args += ids
    if status:
        where.append("m.status=?")
        args.append(status)
    if before_id:
        where.append("m.id<?")
        args.append(before_id)
    sql = (f"SELECT {MATCH_COLS} FROM watch_matches m LEFT JOIN watch_watchlist w ON w.id=m.watch_id "
           "LEFT JOIN watch_items i ON i.id=m.item_id" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY m.id DESC LIMIT ?")
    return [match_dict(r) for r in conn.execute(sql, (*args, limit))]


def list_watchlist(conn: sqlite3.Connection, *, kind: str | None = None, origin: str | None = None,
                   q: str | None = None, limit: int = 500) -> dict[str, Any]:
    where, args = [], []
    if kind:
        where.append("w.kind=?")
        args.append(kind)
    if origin:
        where.append("w.origin=?")
        args.append(origin)
    if q:
        where.append("(w.value LIKE ? OR w.label LIKE ? OR w.campaign_id LIKE ?)")
        args += [f"%{q.lower()}%", f"%{q}%", f"%{q}%"]
    sql = ("SELECT w.id, w.kind, w.display, w.origin, w.campaign_id, w.label, w.created_at, "
           "(SELECT COUNT(*) FROM watch_matches m WHERE m.watch_id=w.id) FROM watch_watchlist w"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY w.origin DESC, w.kind, w.id LIMIT ?")
    entries = [{"id": r[0], "kind": r[1], "kind_pl": ind.KIND_PL.get(r[1], r[1]), "value_display": r[2],
                "origin": r[3], "campaign_id": r[4] or None, "campaign_label": r[5] if r[3] == "campaign" else None,
                "label": r[5], "created_at": r[6], "hits": r[7]} for r in conn.execute(sql, (*args, limit))]
    counts = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM watch_watchlist GROUP BY kind")}
    origins = {r[0]: r[1] for r in conn.execute("SELECT origin, COUNT(*) FROM watch_watchlist GROUP BY origin")}
    return {"entries": entries, "counts": counts, "origins": origins}


def list_runs(conn: sqlite3.Connection, *, source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = ("SELECT id, source, started_at, duration_ms, fetched, new, matched, http_status, not_modified, error, "
           "replay FROM watch_runs" + (" WHERE source=?" if source else "") + " ORDER BY id DESC LIMIT ?")
    args = (source, limit) if source else (limit,)
    return [{"id": r[0], "source": r[1], "started_at": r[2], "duration_ms": r[3], "fetched": r[4], "new": r[5],
             "matched": r[6], "http_status": r[7], "not_modified": bool(r[8]), "error_pl": r[9],
             "replay": bool(r[10])} for r in conn.execute(sql, args)]


def sparkline(conn: sqlite3.Connection, now: float, minutes: int = 60, replay: bool | None = None
              ) -> list[dict[str, Any]]:
    end = datetime.fromtimestamp(now, UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=minutes - 1)
    cond = "" if replay is None else f" AND replay={int(replay)}"
    agg = {r[0]: r[1:] for r in conn.execute(
        "SELECT substr(started_at, 1, 16), SUM(fetched), SUM(new), SUM(matched) FROM watch_runs "
        f"WHERE started_at >= ?{cond} GROUP BY 1", (start.isoformat(timespec="seconds"),))}
    out = []
    for i in range(minutes):
        m = (start + timedelta(minutes=i)).isoformat(timespec="minutes")[:16]
        f, n, mt = agg.get(m, (0, 0, 0))
        out.append({"minute": m, "fetched": f or 0, "new": n or 0, "matched": mt or 0})
    return out


def timeline(conn: sqlite3.Connection, now: float, hours: int = 48, replay: bool | None = None
             ) -> list[dict[str, Any]]:
    end = datetime.fromtimestamp(now, UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours - 1)
    cond = "" if replay is None else f" AND replay={int(replay)}"
    agg = {r[0]: r[1:] for r in conn.execute(
        "SELECT substr(started_at, 1, 13), SUM(new), SUM(matched) FROM watch_runs "
        f"WHERE started_at >= ?{cond} GROUP BY 1", (start.isoformat(timespec="seconds"),))}
    out = []
    for i in range(hours):
        h = (start + timedelta(hours=i)).isoformat(timespec="hours")[:13]
        n, mt = agg.get(h, (0, 0))
        out.append({"hour": h, "new": n or 0, "matched": mt or 0})
    return out


def totals(conn: sqlite3.Connection, now: float) -> dict[str, int]:
    day = iso(now - 86400)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    return {"items": one("SELECT COUNT(*) FROM watch_items"),
            "items_24h": one("SELECT COUNT(*) FROM watch_items WHERE last_seen >= ?", day),
            "matches": one("SELECT COUNT(*) FROM watch_matches"),
            "matches_new": one("SELECT COUNT(*) FROM watch_matches WHERE status='new'"),
            "watchlist": one("SELECT COUNT(*) FROM watch_watchlist")}


def source_stats(conn: sqlite3.Connection, sid: str, now: float, *, replay: bool = False) -> dict[str, Any]:
    """Freshness of one source from its own rows: last time a poll stored new items, newest source timestamp,
    new items in the last 24 h and per hour (24 entries, oldest first)."""
    rep = int(replay)
    last_new = conn.execute("SELECT MAX(started_at) FROM watch_runs WHERE source=? AND replay=? AND new>0",
                            (sid, rep)).fetchone()[0]
    newest = conn.execute("SELECT MAX(first_seen) FROM watch_items WHERE source=? AND replay=?",
                          (sid, rep)).fetchone()[0]
    day = iso(now - 86400)
    n24 = conn.execute("SELECT COALESCE(SUM(new), 0) FROM watch_runs WHERE source=? AND replay=? AND started_at>=?",
                       (sid, rep, day)).fetchone()[0]
    end = datetime.fromtimestamp(now, UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=23)
    agg = dict(conn.execute("SELECT substr(started_at, 1, 13), SUM(new) FROM watch_runs WHERE source=? AND replay=? "
                            "AND started_at>=? GROUP BY 1", (sid, rep, start.isoformat(timespec="seconds"))).fetchall())
    hourly = []
    for i in range(24):
        h = (start + timedelta(hours=i)).isoformat(timespec="hours")[:13]
        hourly.append({"hour": h, "new": int(agg.get(h) or 0)})
    return {"last_new_at": last_new, "newest_entry": newest, "new_24h": int(n24 or 0), "hourly": hourly}
