"""BONUS-AUTO: the automatic search loop as one readable picture (NASK bonus point: "mechanizm automatycznego
wyszukiwania podejrzanych reklam w publicznie dostępnych źródłach ... okresowe aktualizowanie informacji lub
identyfikowanie nowych materiałów powiązanych z wcześniej wykrytymi kampaniami").

Nothing here searches or decides by itself. It reads what the collector (hydra.meta.collector), the analysis worker
(hydra.meta.analysis) and the WATCH-RT engine (hydra.watch.rt) stored, and records one summary row per collection
cycle (`meta_state` key `cycles:<mode>`, last CYCLES_KEEP cycles). Every number comes from the DB.

Also: the campaign probe of the synthetic replay (`hydra_campaign_probe` rows of a `hydra.meta.pages.v1` fixture):
at replay time such a row gets the landing host / operator phone of one EXISTING campaign of the latest analyse run
(local DB only, never written to git and never sent anywhere), so the offline demo shows "nowy materiał powiązany z
kampanią" on whatever base run is loaded. The ad stays labelled DANE SYNTETYCZNE; the assignment is the normal
open-set decision of the assigner (it may still abstain: that is shown, not hidden).
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from typing import Any

from hydra.meta import store

CYCLES_KEEP = 50
SOURCE_PL = {"meta-adlibrary": "Biblioteka reklam Meta (Ad Library API)", "cert-pl": "CERT Polska: lista ostrzeżeń",
             "urlhaus": "URLhaus (abuse.ch)", "openphish": "OpenPhish", "crtsh": "crt.sh (Certificate Transparency)"}
TRIGGER_PL = {"schedule": "harmonogram", "poll_now": "skanuj teraz", "scheduler": "harmonogram (scheduler)",
              "replay": "odtworzenie", "diagnose": "diagnostyka", "expansion": "rozszerzenie"}


# ------------------------------------------------------------------------------------------------ cycles
def cycle_row(res: Any, *, trigger: str, started: str, finished: str) -> dict[str, Any]:
    norm = list(getattr(res, "normalized", []) or [])
    seen = len({(n.get("creative") or {}).get("source_ad_id") for n in norm})
    ended = sum(1 for n in norm if (n.get("meta") or {}).get("source_delivery_stop"))
    new_ads = int(res.new_ads)
    changed = max(0, int(res.new_revisions) - new_ads)  # a new ad is also a first revision
    return {"mode": res.mode, "trigger": trigger, "trigger_pl": TRIGGER_PL.get(trigger, trigger),
            "started_at": started, "finished_at": finished, "status": res.status, "message_pl": res.message_pl,
            "requests": int(res.requests), "pages": int(res.pages), "records": int(res.records), "ads_seen": seen,
            "new_ads": new_ads, "changed_ads": changed, "unchanged_ads": max(0, seen - new_ads - changed),
            "ended_ads": ended, "queued_analyses": len(res.outbox_ids)}


def record_cycle(conn: sqlite3.Connection, row: dict[str, Any], now: float) -> None:
    key = f"cycles:{row['mode']}"
    rows = [row, *(store.get_state(conn, key, []) or [])][:CYCLES_KEEP]
    store.set_state(conn, key, rows, now)


def cycles(conn: sqlite3.Connection, mode: str, limit: int = 10) -> list[dict[str, Any]]:
    return list(store.get_state(conn, f"cycles:{mode}", []) or [])[:limit]


# ------------------------------------------------------------------------------------------------ campaign links
def _labels(conn: sqlite3.Connection, run_id: str | None) -> dict[str, str]:
    if not run_id:
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT id, label FROM campaigns WHERE run_id=?", (run_id,))}
    except sqlite3.Error:
        return {}


def meta_links(conn: sqlite3.Connection, *, mode: str | None = None, campaign_id: str | None = None,
               run_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Collected ads the assigner put into an EXISTING campaign of its base run (decision `assign`), newest first,
    latest analysis per ad revision."""
    if not store.has_schema(conn):
        return []
    where, args = ["m.decision='assign'", "m.hydra_campaign_id IS NOT NULL"], []
    if mode:
        where.append("m.mode=?")
        args.append(mode)
    if campaign_id:
        where.append("m.hydra_campaign_id=?")
        args.append(campaign_id)
    if run_id:
        where.append("m.base_run_id=?")
        args.append(run_id)
    rows = conn.execute(
        "SELECT m.id, m.mode, m.ad_id, m.revision_id, m.incident_id, m.base_run_id, m.hydra_campaign_id, "
        "m.correlation, m.triage_priority, m.review_status, m.created_at, a.page_id, a.page_name, "
        "a.first_observed_at, a.last_observed_at, a.current_rev_id, r.rev FROM meta_analyses m "
        "JOIN meta_ads a ON a.mode=m.mode AND a.ad_id=m.ad_id LEFT JOIN meta_revisions r ON r.id=m.revision_id "
        f"WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?", (*args, limit * 3)).fetchall()
    out, seen = [], set()
    labels: dict[str, dict[str, str]] = {}
    for (aid, md, ad_id, rid, iid, base, cid, corr, pr, rv, created, page_id, page_name, first, last, cur,
         rev) in rows:
        if (md, ad_id, rid) in seen:
            continue
        seen.add((md, ad_id, rid))
        c = json.loads(corr or "{}")
        cand = next((x for x in c.get("candidates") or [] if x.get("campaign_id") == cid), None)
        lab = labels.setdefault(base or "", _labels(conn, base))
        out.append({
            "analysis_id": aid, "mode": md, "mode_label_pl": store.MODE_PL.get(md, md), "ad_id": ad_id,
            "revision": rev, "current_revision": rid == cur, "incident_id": iid, "base_run_id": base,
            "campaign_id": cid, "campaign_label": lab.get(cid), "page_id": page_id, "page_name": page_name,
            "first_observed_at": first, "last_observed_at": last, "analysed_at": created,
            "triage_priority": pr, "triage_priority_pl": store.PRIORITY_PL.get(pr, pr), "review_status": rv,
            "total_bits": cand.get("total_bits") if cand else None,
            "probability": cand.get("probability") if cand else None,
            "evidence": [{"kind": e.get("kind"), "value": e.get("value"), "bits": e.get("bits")}
                         for e in (c.get("evidence") or [])[:6]],
            "reason_pl": c.get("reason_pl"), "library_link": None if md != "live" else
            f"https://www.facebook.com/ads/library/?id={ad_id}"})
        if len(out) >= limit:
            break
    return out


def watch_links(conn: sqlite3.Connection, *, campaign_id: str | None = None, run_id: str | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
    """WATCH-RT matches (CERT Polska, URLhaus, OpenPhish, crt.sh, Meta) against campaign-derived indicators."""
    try:
        where, args = ["m.campaign_id IS NOT NULL AND m.campaign_id != ''"], []
        if campaign_id:
            where.append("m.campaign_id=?")
            args.append(campaign_id)
        if run_id:
            where.append("(m.run_id=? OR m.run_id IS NULL)")
            args.append(run_id)
        rows = conn.execute(
            "SELECT m.id, m.source, m.indicator_kind, m.indicator_value, m.campaign_id, m.run_id, m.strength, m.bits, "
            "m.status, m.replay, m.created_at, i.value, i.first_seen, w.display, w.label FROM watch_matches m "
            "LEFT JOIN watch_items i ON i.id=m.item_id LEFT JOIN watch_watchlist w ON w.id=m.watch_id "
            f"WHERE {' AND '.join(where)} ORDER BY m.id DESC LIMIT ?", (*args, limit)).fetchall()
    except sqlite3.Error:
        return []
    from hydra.watch.rt import indicators as ind

    out = []
    for mid, src, kind, value, cid, run, strength, bits, status, replay, created, item, first, display, label in rows:
        out.append({"match_id": mid, "source": src, "source_pl": SOURCE_PL.get(src, src), "indicator_kind": kind,
                    "indicator_display": display or ind.display(kind, value), "campaign_id": cid,
                    "campaign_label": label, "run_id": run, "strength": strength, "bits": bits, "status": status,
                    "replay": bool(replay), "matched_at": created, "item_first_seen": first,
                    "item_defanged": (ind.display("url" if "://" in item else "host", item) if item else None)})
    return out


def campaign_links(conn: sqlite3.Connection, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    meta = meta_links(conn, campaign_id=campaign_id, run_id=run_id, limit=50)
    watch = watch_links(conn, campaign_id=campaign_id, run_id=run_id, limit=50)
    return {"campaign_id": campaign_id, "run_id": run_id, "meta_ads": meta, "watch_matches": watch,
            "note_pl": ("Nowe materiały wykryte automatycznie po analizie bazowej. Reklama „przypisana” to decyzja "
                        "przypisania HYDRY (otwarty zbiór, bity dowodu), nie potwierdzenie analityka; dopasowanie "
                        "WATCH-RT to wspólny wskaźnik (domena, host, telefon), kandydat do sprawdzenia.")}


# ------------------------------------------------------------------------------------------------ overview
def triage_distribution(conn: sqlite3.Connection, mode: str) -> dict[str, int]:
    out = {k: 0 for k in store.PRIORITY_PL}
    for pr, n in conn.execute("SELECT triage_priority, COUNT(*) FROM meta_analyses WHERE mode=? GROUP BY 1", (mode,)):
        out[pr] = n
    return out


def sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT id, enabled, state, interval_s, next_due, last_poll, last_ok, total_polls, "
                            "total_items, last_error FROM watch_sources ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    cols = ("id", "enabled", "state", "interval_s", "next_due", "last_poll", "last_ok", "total_polls", "total_items",
            "last_error")
    return [dict(zip(cols, r, strict=True)) | {"enabled": bool(r[1]), "label_pl": SOURCE_PL.get(r[0], r[0])}
            for r in rows]


def watch_match_counts(conn: sqlite3.Connection) -> dict[str, int]:
    try:
        r = conn.execute("SELECT COUNT(*), SUM(COALESCE(campaign_id, '') != ''), SUM(status='new'), "
                         "SUM(status='new' AND COALESCE(campaign_id, '') != '') FROM watch_matches").fetchone()
    except sqlite3.Error:
        return {"total": 0, "to_campaigns": 0, "new": 0, "new_to_campaigns": 0}
    return {"total": int(r[0] or 0), "to_campaigns": int(r[1] or 0), "new": int(r[2] or 0),
            "new_to_campaigns": int(r[3] or 0)}


def rules_view(cfg: dict[str, Any]) -> dict[str, Any]:
    from hydra.enrich.adlibrary import DEFAULT_KEYWORDS

    qs = [q for q in cfg["queries"] if q.get("origin") != "replay"]
    r = cfg.get("rules") or {}
    return {
        "keywords_sent": [q["value"] for q in qs if q["kind"] == "keyword" and q["enabled"]],
        "page_ids_sent": [q["value"] for q in qs if q["kind"] == "page_ids" and q["enabled"]],
        "countries": cfg["countries"], "ad_active_status": cfg["ad_active_status"], "languages": cfg["languages"],
        "date_window_days": cfg["date_window_days"], "overlap_days": cfg["overlap_days"],
        "date_from": cfg.get("date_from"), "date_to": cfg.get("date_to"), "platforms": cfg.get("platforms") or [],
        "local_keywords_default": len(DEFAULT_KEYWORDS), "local_keywords": r.get("keywords") or [],
        "celebrities": r.get("celebrities") or [], "page_name_patterns": r.get("page_name_patterns") or [],
        "same_text_min_pages": r.get("same_text_min_pages") or 3,
        "alert_min_priority": r.get("alert_min_priority") or "medium",
        "expansion": cfg.get("expansion") or {},
    }


def overview(conn: sqlite3.Connection, mode: str) -> dict[str, Any]:
    cfg = store.config(conn)
    cyc = cycles(conn, mode, 10)
    c = store.counters(conn, mode)
    links = meta_links(conn, mode=mode, limit=20)
    return {
        "mode": mode, "mode_label_pl": store.MODE_PL[mode],
        "rules": rules_view(cfg),
        "sources": sources(conn),
        "cycles": cyc,
        "last_update": cyc[0]["finished_at"] if cyc else None,
        "totals": {
            "cycles_recorded": len(store.get_state(conn, f"cycles:{mode}", []) or []),
            "unique_ads": c["unique_ads"], "records_fetched": c["records_fetched"],
            "new_revisions": c["new_revisions"], "analyses_done": c["analyses_done"],
            "analyses_partial": c["analyses_partial"],
            "assigned": c["assigned"], "new_patterns": c["new_patterns"], "abstained": c["abstained"],
            "alerts_open": c["alerts_open"], "analyst_confirmed": c["analyst_confirmed"],
            "analyst_dismissed": c["analyst_dismissed"],
            "linked_campaigns": len({x["campaign_id"] for x in meta_links(conn, mode=mode, limit=500)}),
        },
        "queue": store.queue_counts(conn, mode),
        "triage": triage_distribution(conn, mode),
        "linked_ads": links,
        "watch_matches": {"counts": watch_match_counts(conn), "latest": watch_links(conn, limit=10)},
    }


# ------------------------------------------------------------------------------------------------ replay probe
PROBE_KINDS = {"landing_host": "hosts", "registrable_domain": "hosts", "entry_url": "urls", "entity_phone": "phones",
               "tracker_id": "trackers", "tds_value": "tds", "headline": "phrases", "ad_headline": "phrases",
               "phrase": "phrases", "product_name": "phrases"}


PROBE_KINDS = {"landing_host": "hosts", "registrable_domain": "hosts", "entry_url": "urls", "entity_phone": "phones",
               "tracker_id": "trackers", "tds_value": "tds", "headline": "headlines", "ad_headline": "headlines",
               "product_name": "headlines", "phrase": "phrases"}
_IBAN_RE = re.compile(r"\bpl\d{26}\b", re.I)


PROBE_KINDS = {"landing_host": "hosts", "registrable_domain": "hosts", "entry_url": "urls", "entity_phone": "phones",
               "tracker_id": "trackers", "tds_value": "tds", "headline": "headlines", "ad_headline": "headlines",
               "product_name": "headlines", "phrase": "phrases"}
_IBAN_RE = re.compile(r"\bpl\d{26}\b", re.I)


def campaign_probe(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Indicators of one EXISTING campaign of the latest analyse run, for the synthetic replay row marked
    `hydra_campaign_probe`: landing host / entry URL, operator phone, tracker id, campaign URL parameter and the
    campaign's most shared text phrases, taken from the genes of its members (read-only, local DB; values stored as
    hashes are never used). The campaign with the most members, then the most shared values, wins; singleton
    campaigns are skipped."""
    try:
        from hydra.watch.rt.watchlist import _is_platform, latest_run

        run = latest_run(conn)
        if not run:
            return None
        members: dict[str, list[str]] = {}
        for cid, iid in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id=?", (run,)):
            members.setdefault(cid, []).append(iid)
        kinds = tuple(PROBE_KINDS)
        best: tuple[Any, ...] | None = None
        for cid, incs in sorted(members.items()):
            if len(incs) < 2:
                continue
            q, k = ",".join("?" * len(incs)), ",".join("?" * len(kinds))
            counts: dict[tuple[str, str], set[str]] = {}
            for iid, kind, value, dep in conn.execute(
                    f"SELECT incident_id, kind, value, dependency_group FROM genes WHERE incident_id IN ({q}) AND kind "
                    f"IN ({k}) AND value IS NOT NULL AND status='observed'", (*incs, *kinds)):
                v, dg = str(value), str(dep or "")
                if v.startswith("h:") or len(v) > 120 or ":boilerplate:" in dg or ":platform:" in dg:
                    continue
                if PROBE_KINDS[kind] == "hosts" and _is_platform(v):
                    continue
                counts.setdefault((PROBE_KINDS[kind], v), set()).add(iid)
            d: dict[str, Any] = {"campaign_id": cid, "run_id": run, "members": len(incs), "hosts": [], "urls": [],
                                 "phones": [], "trackers": [], "tds": [], "headlines": [], "phrases": [],
                                 "ibans": []}
            for (slot, v), _who in sorted(counts.items(), key=lambda x: (-len(x[1]), x[0])):
                if v not in d[slot]:
                    d[slot].append(v)
                for m in _IBAN_RE.findall(v) if slot == "phrases" else []:
                    if m.upper() not in d["ibans"]:
                        d["ibans"].append(m.upper())
            if not d["hosts"]:
                continue
            with contextlib.suppress(sqlite3.Error):  # one ad image of a member (local CAS bytes, replay only)
                img = conn.execute(
                    f"SELECT a.id FROM incident_artifacts ia JOIN artifacts a ON a.id=ia.artifact_id WHERE "
                    f"ia.incident_id IN ({q}) AND ia.role='ad_creative' AND a.mime IN ('image/png', 'image/jpeg') "
                    "AND a.size < 4000000 ORDER BY ia.incident_id LIMIT 1", incs).fetchone()
                d["image"] = img[0] if img else None
            score =(len(incs), sum(1 for who in counts.values() if len(who) >= 2))
            if best is None or score > best[0]:
                best = (score, d)
        return best[1] if best else None
    except Exception:  # noqa: BLE001
        return None


def probe_row(row: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    host = sorted(probe["hosts"], key=len)[0]
    url = next((u if "://" in u else f"https://{u}" for u in probe.get("urls") or [] if host in u), f"https://{host}/")
    if probe.get("tds"):
        url += ("&" if "?" in url else "?") + "&".join(probe["tds"][:2])
    phone = (probe.get("phones") or [None])[0]
    tracker = next((t.split(":", 1)[1] for t in probe.get("trackers") or [] if ":" in t), None)
    text = [*(probe.get("headlines") or [])[:2], *(probe.get("phrases") or [])[:3]]
    phrases = "; ".join(p.capitalize() for p in text)
    iban = (probe.get("ibans") or [None])[0]
    body = (f"SYNTETYCZNA reklama demonstracyjna (sonda kampanii {probe['campaign_id']}). "
            + (f"{phrases}. " if phrases else "") + f"Sprawdź ofertę {url}"
            + (f" Kontakt {phone}" if phone else "") + (f" Przelew na {iban}." if iban else "")
            + (f" fbq('init','{tracker}')" if tracker else ""))
    out = dict(row)
    out["ad_creative_bodies"] = [body]
    out["ad_creative_link_titles"] = [(text or ["Oferta"])[0].capitalize() + " (SYNTETYCZNE)"]
    out["ad_creative_link_captions"] = [host.upper()]
    out.pop("hydra_landing_urls", None)  # the URL is in the text: one landing URL = URL artefact (entry URL genes)
    out.pop("hydra_campaign_probe", None)
    if probe.get("image_b64"):
        out["hydra_synthetic_media"] = [{"b64": probe["image_b64"], "note": "obraz członka istniejącej kampanii"}]
    return out
