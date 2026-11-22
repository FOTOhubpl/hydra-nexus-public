"""WATCH-RT sources: one adapter per public feed. An adapter turns one poll (zero or more HTTP requests made through
the caller's `fetch` function) into raw items plus the new cursor / validators. It never opens a socket itself:

- live: `fetch` = `hydra.integrations.http.request` (offline kill switch, connector enabled flag, netguard public
  address check, per-connector rate limit, audited `INTEGRATION_REQUEST` row) — see engine.LiveFetcher;
- replay: `fetch` = fixture snapshots (engine.ReplayFetcher), the same parsing code path.

What leaves the machine per poll (also in `sends_pl`): CERT Polska / URLhaus / OpenPhish are bulk downloads of a
public list (nothing about our indicators is sent; matching is local). crt.sh receives the operator's manual CT
keywords only, never campaign-derived values. Meta Ad Library receives the operator's manual keywords / page ids only
(hydra.meta.collector, through the same `fetch`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from hydra.redact import redact_url
from hydra.watch.rt import indicators as ind


class SourceError(Exception):
    """A poll failure. `status` is a machine id (offline, disabled, blocked, rate_limited, needs_key, http, parse,
    error); `message_pl` is safe to show (no secrets)."""

    def __init__(self, status: str, message_pl: str, http_status: int | None = None) -> None:
        super().__init__(message_pl)
        self.status = status
        self.message_pl = message_pl
        self.http_status = http_status


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes
    ms: int = 0


# (connector_id, method, url, headers) -> Resp ; raises SourceError
Fetch = Callable[[str, str, str, dict[str, str]], Resp]


@dataclass
class RawItem:
    external_id: str
    kind: str  # url | domain | cert | ad
    value: str
    indicators: list[tuple[str, str]]
    first_seen: str
    payload: dict[str, Any]

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.payload, sort_keys=True, ensure_ascii=False,
                                         default=str).encode()).hexdigest()


@dataclass
class PollCtx:
    fetch: Fetch
    cursor: dict[str, Any]
    etag: str | None = None
    last_modified: str | None = None
    keywords: list[str] = field(default_factory=list)
    secrets: dict[str, str | None] = field(default_factory=dict)
    settings: Any = None
    writer: Any = None
    replay: bool = False
    now: str = ""
    trigger: str = "schedule"  # schedule | poll_now (META-COLLECT poll history)
    clock: Any = None  # engine clock (FakeClock in tests); None = time.time


@dataclass
class PollOut:
    items: list[RawItem] = field(default_factory=list)
    cursor: dict[str, Any] | None = None
    etag: str | None = None
    last_modified: str | None = None
    http_status: int | None = None
    not_modified: bool = False
    note_pl: str | None = None
    detail_pl: str | None = None  # e.g. CERT resync: recorded on the run and in the audit log


def _iso(ts: Any) -> str | None:
    if ts in (None, ""):
        return None
    s = str(ts).strip().replace(" UTC", "+00:00")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    d = d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    return d.isoformat(timespec="seconds")


def _sha(s: str, n: int = 20) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def _cond_headers(ctx: PollCtx) -> dict[str, str]:
    h = {}
    if ctx.etag:
        h["If-None-Match"] = ctx.etag
    if ctx.last_modified:
        h["If-Modified-Since"] = ctx.last_modified
    return h


@dataclass(frozen=True)
class Adapter:
    id: str
    name_pl: str
    connector_id: str
    default_interval_s: int
    min_interval_s: int
    requests_per_poll: int
    sends_pl: str
    poll: Callable[[PollCtx], PollOut]
    secret: str | None = None  # secret name required for live polls (integrations secret store)
    description_pl: str = ""


# ------------------------------------------------------------------------------------------------ CERT Polska
CERT_URL = "https://hole.cert.pl/domains/v2/actions_{year}.log"
CERT_BACKFILL_BYTES = 16384


def parse_cert_lines(text: str, *, drop_first: bool = False) -> list[RawItem]:
    lines = text.split("\n")
    if drop_first and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict) or str(raw.get("ActionType") or "block").lower() != "block":
            continue
        host = ind.norm_host(str(raw.get("DomainAddress") or ""))
        if not host:
            continue
        first = _iso(raw.get("ActionTime")) or datetime.now(UTC).isoformat(timespec="seconds")
        ref = str(raw.get("RegisterPositionId") or _sha(f"{host}|{first}"))
        out.append(RawItem(f"cert:{ref}", "domain", host, ind.from_host(host), first,
                           {"DomainAddress": host, "ActionTime": raw.get("ActionTime"), "ActionType": "block",
                            "RegisterPositionId": raw.get("RegisterPositionId")}))
    return out


def content_range(headers: dict[str, str]) -> tuple[int | None, int | None]:
    """(start, total) from `Content-Range: bytes 10-20/31602124` or `bytes */31602124` (416); None = absent."""
    m = re.match(r"\s*bytes\s+(?:(\d+)-\d+|\*)/(\d+|\*)", headers.get("content-range") or "")
    if not m:
        return None, None
    return (int(m.group(1)) if m.group(1) else None), (int(m.group(2)) if m.group(2).isdigit() else None)


RESYNC_SHRUNK_PL = "plik CERT skrócony: resynchronizacja"
RESYNC_SHIFT_PL = "odpowiedź CERT od innej pozycji niż żądana: resynchronizacja"
RESYNC_GAP_PL = "zaległość w pliku CERT większa niż limit: pominięto starsze wpisy, czytanie od końca pliku"
# Every GET asks for a bounded byte range, so one poll never exceeds the response cap of the audited HTTP path
# (2026-11-22: a cursor reset to 9 564 B made `bytes=9564-` return 31.6 MB > 20 MB and opened the circuit).
CERT_CHUNK_BYTES = 4_000_000
CERT_MAX_GAP_BYTES = 8_000_000


def _resync(cur: dict[str, Any], total: int | None) -> None:
    start = max(0, int(total or 0) - CERT_BACKFILL_BYTES)
    cur.update(offset=start, aligned=start == 0, partial="")


def poll_cert(ctx: PollCtx) -> PollOut:
    """Range tail of the append-only actions log. 416 with `Content-Range: bytes */N`: N == offset -> nothing new
    (the file is regenerated every few minutes with the same bytes, Last-Modified changes); N != offset (rotated or
    shrunk) -> resync to N - 16 KB and poll again at once (items dedup by external_id, so this is idempotent).
    206 whose start differs from the requested offset -> resync as well."""
    year = (ctx.now or datetime.now(UTC).isoformat())[:4]
    url = CERT_URL.format(year=year)
    cur = dict(ctx.cursor or {})
    if cur.get("url") != url:
        head = ctx.fetch("cert-pl", "HEAD", url, {"Accept": "*/*"})
        if head.status != 200:
            raise SourceError("http", f"CERT Polska: HEAD zwrócił HTTP {head.status}.", head.status)
        size = int(head.headers.get("content-length") or 0)
        cur = {"url": url, "partial": "", "file_size": size}
        _resync(cur, size)
    detail: str | None = None
    for _attempt in range(2):
        offset = int(cur.get("offset") or 0)
        known = int(cur.get("file_size") or 0)
        if known - offset > CERT_MAX_GAP_BYTES:
            detail = RESYNC_GAP_PL
            _resync(cur, known)
            offset = int(cur.get("offset") or 0)
        r = ctx.fetch("cert-pl", "GET", url, {"Range": f"bytes={offset}-{offset + CERT_CHUNK_BYTES - 1}",
                                              "Accept": "*/*"})
        lm = r.headers.get("last-modified")
        if lm:
            cur["server_last_modified"] = lm
        start, total = content_range(r.headers)
        if r.status == 416:
            if total is None or total == offset:
                cur["file_size"] = total if total is not None else cur.get("file_size")
                return PollOut([], cur, http_status=416, not_modified=True, detail_pl=detail)
            detail = RESYNC_SHRUNK_PL if total < offset else RESYNC_SHIFT_PL
            _resync(cur, total)
            continue
        if r.status == 304:
            return PollOut([], cur, http_status=304, not_modified=True, detail_pl=detail)
        if r.status == 206 and start is not None and start != offset:
            detail = RESYNC_SHIFT_PL
            _resync(cur, total if total is not None else start)
            continue
        if r.status == 206 and total is not None and total - offset > CERT_MAX_GAP_BYTES:
            detail = RESYNC_GAP_PL
            cur["file_size"] = total
            _resync(cur, total)
            continue
        body = r.body
        if r.status == 200 and offset > 0:
            if len(body) < offset:  # full body shorter than our position: the file was replaced
                detail = RESYNC_SHRUNK_PL
                _resync(cur, len(body))
                continue
            body = body[offset:offset + CERT_CHUNK_BYTES]  # Range ignored: the log is append-only
        elif r.status not in (200, 206):
            raise SourceError("http", f"CERT Polska: HTTP {r.status}.", r.status)
        text = str(cur.get("partial") or "") + body.decode("utf-8", "replace")
        nl = text.rfind("\n")
        complete, partial = (text[: nl + 1], text[nl + 1:]) if nl >= 0 else ("", text)
        items = parse_cert_lines(complete, drop_first=not cur.get("aligned"))
        if partial.strip():
            try:
                json.loads(partial)
                items += parse_cert_lines(partial)
                partial = ""
            except ValueError:
                pass
        cur.update(offset=offset + len(body), aligned=True, partial=partial[-4096:],
                   file_size=total if total is not None else offset + len(body))
        newest = max([str(cur.get("newest_entry") or "")] + [it.first_seen for it in items])
        if newest:
            cur["newest_entry"] = newest
        return PollOut(items, cur, etag=r.headers.get("etag"), http_status=r.status, detail_pl=detail)
    raise SourceError("http", "CERT Polska: resynchronizacja pozycji w pliku nie powiodła się (dwie próby).")


# ------------------------------------------------------------------------------------------------ URLhaus
URLHAUS_RECENT = "https://urlhaus-api.abuse.ch/v1/urls/recent/limit/1000/"


def parse_urlhaus(data: Any) -> list[RawItem]:
    if not isinstance(data, dict):
        raise SourceError("parse", "URLhaus: odpowiedź nie jest obiektem JSON.")
    qs = data.get("query_status")
    if qs == "no_results":
        return []
    if qs != "ok":
        raise SourceError("http", f"URLhaus: query_status={str(qs)[:40]}.")
    out = []
    for u in data.get("urls") or []:
        if not isinstance(u, dict):
            continue
        url = redact_url(str(u.get("url") or "")) or ""
        pairs = ind.from_url(url) or ind.from_host(u.get("host"))
        if not pairs:
            continue
        tags = [str(t) for t in (u.get("tags") or [])][:20]
        pairs += ind.from_text(" ".join(tags) + " " + urlsplit(url).query.replace("=", " ").replace("&", " "))
        out.append(RawItem(f"urlhaus:{u.get('id') or _sha(url)}", "url", ind.norm_url(url) or url,
                           ind.dedup(pairs), _iso(u.get("date_added")) or datetime.now(UTC).isoformat(
                               timespec="seconds"),
                           {"url": url, "url_status": u.get("url_status"), "threat": u.get("threat"), "tags": tags,
                            "reference": u.get("urlhaus_reference")}))
    return out


def poll_urlhaus(ctx: PollCtx) -> PollOut:
    key = ctx.secrets.get("URLHAUS_AUTH_KEY")
    if not key and not ctx.replay:
        raise SourceError("needs_key", "URLhaus wymaga klucza Auth-Key (Integracje → URLhaus → klucz "
                                       "URLHAUS_AUTH_KEY).")
    r = ctx.fetch("urlhaus", "GET", URLHAUS_RECENT, {"Auth-Key": key or "", **_cond_headers(ctx)})
    if r.status == 304:
        return PollOut([], ctx.cursor, ctx.etag, ctx.last_modified, 304, True)
    if r.status != 200:
        raise SourceError("http", f"URLhaus: HTTP {r.status}.", r.status)
    try:
        data = json.loads(r.body or b"null")
    except ValueError:
        raise SourceError("parse", "URLhaus: odpowiedź nie jest poprawnym JSON.") from None
    return PollOut(parse_urlhaus(data), ctx.cursor, r.headers.get("etag"), r.headers.get("last-modified"), 200)


# ------------------------------------------------------------------------------------------------ OpenPhish
def parse_openphish(text: str) -> list[RawItem]:
    out = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for line in text.splitlines():
        u = line.strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        url = redact_url(u) or u
        pairs = ind.from_url(url)
        if not pairs:
            continue
        pairs = ind.dedup(pairs + ind.from_text(urlsplit(url).query.replace("=", " ").replace("&", " ")))
        out.append(RawItem(f"openphish:{_sha(url)}", "url", ind.norm_url(url) or url, pairs, now, {"url": url}))
    return out


def poll_openphish(ctx: PollCtx) -> PollOut:
    from hydra.integrations.clients.openphish import FEED_URL

    r = ctx.fetch("openphish", "GET", FEED_URL, {"Accept": "text/plain", **_cond_headers(ctx)})
    if r.status == 304:
        return PollOut([], ctx.cursor, ctx.etag, ctx.last_modified, 304, True)
    if r.status != 200:
        raise SourceError("http", f"OpenPhish: HTTP {r.status}.", r.status)
    return PollOut(parse_openphish(r.body.decode("utf-8", "replace")), ctx.cursor, r.headers.get("etag"),
                   r.headers.get("last-modified"), 200)


# ------------------------------------------------------------------------------------------------ crt.sh (CT)
CRTSH_PER_POLL = 3
CRTSH_FIRST_POLL_MAX = 50


def parse_crtsh(data: Any, keyword: str, min_id: int) -> tuple[list[RawItem], int]:
    if not isinstance(data, list):
        raise SourceError("parse", "crt.sh: odpowiedź nie jest listą JSON.")
    rows = [c for c in data if isinstance(c, dict) and str(c.get("id") or "").isdigit()]
    rows.sort(key=lambda c: int(c["id"]))
    max_id = max([min_id] + [int(c["id"]) for c in rows])
    if min_id == 0:
        rows = rows[-CRTSH_FIRST_POLL_MAX:]  # first poll of a keyword: newest certificates only
    else:
        rows = [c for c in rows if int(c["id"]) > min_id]
    out = []
    for c in rows:
        names = sorted({n.strip().lower().removeprefix("*.") for n in str(c.get("name_value") or "").split("\n")
                        if n.strip()})[:50]
        pairs: list[tuple[str, str]] = []
        for n in names:
            pairs += ind.from_host(n)
        if not pairs:
            continue
        first = _iso(c.get("entry_timestamp")) or _iso(c.get("not_before")) or datetime.now(UTC).isoformat(
            timespec="seconds")
        out.append(RawItem(f"crtsh:{c['id']}", "cert", names[0] if names else str(c["id"]), ind.dedup(pairs), first,
                           {"id": c["id"], "names": names, "issuer": str(c.get("issuer_name") or "")[:200],
                            "not_before": c.get("not_before"), "keyword": keyword}))
    return out, max_id


def poll_crtsh(ctx: PollCtx) -> PollOut:
    kws = list(ctx.keywords)
    cur = dict(ctx.cursor or {})
    if not kws:
        return PollOut([], cur, note_pl="Brak słów kluczowych CT: dodaj ręczny wpis typu „słowo kluczowe (CT)”.")
    rr = int(cur.get("rr") or 0)
    ids = dict(cur.get("max_id") or {})
    items: list[RawItem] = []
    status = None
    for i in range(min(CRTSH_PER_POLL, len(kws))):
        kw = kws[(rr + i) % len(kws)]
        url = f"https://crt.sh/?q={quote('%' + kw + '%')}&output=json&exclude=expired"
        r = ctx.fetch("crtsh", "GET", url, {"Accept": "application/json"})
        status = r.status
        if r.status != 200:
            raise SourceError("http", f"crt.sh: HTTP {r.status} (usługa bez SLA, częste 502).", r.status)
        try:
            data = json.loads(r.body or b"[]")
        except ValueError:
            raise SourceError("parse", "crt.sh: odpowiedź nie jest poprawnym JSON.") from None
        got, ids[kw] = parse_crtsh(data, kw, int(ids.get(kw) or 0))
        items += got
    cur.update(rr=(rr + CRTSH_PER_POLL) % max(1, len(kws)), max_id=ids)
    return PollOut(items, cur, http_status=status)


# ------------------------------------------------------------------------------------------------ Meta Ad Library
def parse_ads(metas: list[dict[str, Any]]) -> list[RawItem]:
    out = []
    for m in metas:
        ad_id = str(m.get("id") or "").strip()
        if not ad_id:
            continue
        pairs: list[tuple[str, str]] = [("ad_id", ad_id)]
        if m.get("page_id"):
            pairs.append(("page_id", str(m["page_id"])))
        for u in m.get("link_urls") or []:
            pairs += ind.from_url(str(u))
        pairs += ind.from_text(str(m.get("text") or ""))
        first = _iso(m.get("ad_delivery_start_time")) or _iso(m.get("fetched_at")) or datetime.now(UTC).isoformat(
            timespec="seconds")
        out.append(RawItem(f"meta:{ad_id}", "ad", ad_id, ind.dedup(pairs), first,
                           {"id": ad_id, "page_id": m.get("page_id"), "page_name": m.get("page_name"),
                            "link_urls": [redact_url(str(u)) for u in m.get("link_urls") or []][:10],
                            "keyword": m.get("keyword")}))
    return out


def meta_items(normalized: list[dict[str, Any]], mode: str) -> list[RawItem]:
    """Normalised META-COLLECT ads -> WATCH-RT feed items (watchlist matching on ad/page id, landing URLs, text)."""
    metas = []
    for n in normalized:
        cr, meta = n["creative"], n["meta"]
        metas.append({"id": cr["source_ad_id"], "page_id": cr.get("source_page_id"),
                      "page_name": cr.get("source_page_name"),
                      "link_urls": [u["url"] for u in cr.get("landing_urls") or []],
                      "text": " ".join([*cr.get("creative_bodies", []), *cr.get("creative_link_titles", [])])[:5000],
                      "ad_delivery_start_time": meta.get("source_delivery_start"), "mode": mode})
    return parse_ads(metas)


META_DEFER = {"deferred_budget", "busy", "idle", "empty", "ok", "partial"}


def poll_meta(ctx: PollCtx) -> PollOut:
    """META-COLLECT (hydra/meta): the only Meta collector. Live: `hydra.meta.collector.run_cycle` with this poll's
    `ctx.fetch` (the engine's audited http.request path), HYDRA's own budget, per-query cursors and the durable
    analysis outbox. Replay: the tick body (legacy flat list or `hydra.meta.pages.v1`) through the same normalisation
    and store, in the synthetic / recorded data space. A partial cycle stays visible (note + meta_polls), never a
    plain HTTP 200."""
    import time as _time

    from hydra.meta import collector

    clock = ctx.clock or _time.time
    if ctx.replay:
        r = ctx.fetch("meta-adlibrary", "GET", "fixture://meta-adlibrary", {})
        if r.status == 304 or not r.body:
            return PollOut([], ctx.cursor, http_status=304, not_modified=True)
        res = collector.replay_body(ctx.settings, ctx.writer, r.body, now=clock())
        if res.status == "error":
            raise SourceError("parse", res.message_pl)
        return PollOut(meta_items(res.normalized, res.mode), ctx.cursor, http_status=r.status,
                       note_pl=f"{res.message_pl}: {res.records} rekordów, {res.new_revisions} nowych rewizji.")
    res = collector.run_cycle(ctx.settings, ctx.writer, fetch=ctx.fetch, trigger=ctx.trigger, clock=clock,
                              recorder=collector.recorder_for(ctx.settings))
    if res.status == "needs_config":
        raise SourceError("needs_key", res.message_pl)
    if res.status == "offline":
        raise SourceError("offline", res.message_pl)
    if res.status == "rate_limited":
        raise SourceError("rate_limited", res.message_pl)
    if res.status not in META_DEFER:
        raise SourceError("error", res.message_pl)
    note = (f"{res.message_pl} Rekordy {res.records}, nowe reklamy {res.new_ads}, nowe rewizje {res.new_revisions}, "
            f"żądania {res.requests}.")
    return PollOut(meta_items(res.normalized, "live"), ctx.cursor,
                   http_status=200 if res.status in ("ok", "empty") else None, note_pl=note)


ADAPTERS: dict[str, Adapter] = {a.id: a for a in (
    Adapter("cert-pl", "CERT Polska — lista ostrzeżeń", "cert-pl", 60, 60, 2,
            "Nic o naszych wskaźnikach: pobranie przyrostu publicznego pliku actions_<rok>.log (nagłówek Range).",
            poll_cert, description_pl="Nowe wpisy (blokady) listy ostrzeżeń od ostatniego odpytania."),
    Adapter("urlhaus", "URLhaus (abuse.ch) — najnowsze URL", "urlhaus", 60, 60, 1,
            "Nic o naszych wskaźnikach: pobranie listy ostatnich URL (klucz Auth-Key w nagłówku).",
            poll_urlhaus, secret="URLHAUS_AUTH_KEY", description_pl="Ostatnie złośliwe URL; dopasowanie lokalne."),
    Adapter("openphish", "OpenPhish — feed społecznościowy", "openphish", 3600, 3600, 1,
            "Nic o naszych wskaźnikach: pobranie publicznego pliku feed.txt (If-None-Match).",
            poll_openphish, description_pl="Feed aktualizowany co 12 h; HYDRA pobiera najwyżej raz na godzinę."),
    Adapter("crtsh", "crt.sh — nowe certyfikaty (CT)", "crtsh", 60, 60, CRTSH_PER_POLL,
            "Do crt.sh trafiają tylko ręcznie wpisane słowa kluczowe CT (najwyżej 3 na odpytanie); nigdy wartości "
            "z kampanii.", poll_crtsh, description_pl="Certyfikaty z nazwami zawierającymi słowa kluczowe."),
    Adapter("meta-adlibrary", "Meta Ad Library — reklamy", "meta-adlibrary", 1800, 900, 20,
            "Do Meta trafiają tylko ręcznie wpisane hasła i identyfikatory stron (konfiguracja „Reklamy Meta”), kraj "
            "PL; nigdy frazy z danych organizatora ani kampanii.",
            poll_meta, secret="META_AD_LIBRARY_TOKEN",
            description_pl="Kolektor reklam Meta (META-COLLECT): rotacja haseł, własny budżet, trwała kolejka analiz."),
)}


def effective_min_interval(a: Adapter) -> int:
    """Provider policy (adapter) and the registry rate limit: requests_per_poll must fit in the per-minute limit."""
    from hydra.integrations import registry

    c = registry.get(a.connector_id)
    lim = getattr(c, "rate_limit_per_min", None) if c else None
    rl = int(60.0 * a.requests_per_poll / lim) if lim else 0
    return max(a.min_interval_s, rl)


def fixture_files(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
