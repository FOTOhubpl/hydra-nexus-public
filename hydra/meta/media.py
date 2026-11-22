"""Asset acquisition for one revision: explicit statuses for every material, never a silent gap.

What the documentation allows (S2, ArchivedAd.ad_snapshot_url, read 2026-11-22): the snapshot URL "displays
uncompressed images and videos from the ad. While you cannot currently download a batch of archived ads, you can
download ad creative such as images and text for an individual ad. If you do so, it must be for analysis and you
must comply with the data storage terms in our Terms of Service." HYDRA therefore:

- never downloads in bulk: at most `max_media_per_ad` files per revision, inside the shared Meta budget;
- downloads only with the explicit, default-off opt-in `media_fetch` (operator decision, recorded in the audit log by
  the config change) and never without a live, online collector;
- fetches the snapshot page without JavaScript through the shared transport; when the page needs JS/login it records
  `blocked_or_login_required` and does not try to work around it (no browser, no scraping of protected content);
- accepts files only from Meta CDN hosts (`*.fbcdn.net`, `*.facebook.com`, `*.cdninstagram.com`), MIME by content,
  size/pixel limits, decoder check; everything else is `invalid_or_unsupported_media`;
- never executes HTML/SVG: the snapshot page is parsed as text and not stored as an artifact.
Analyst attachments (`attach`) and synthetic fixture media (`synthetic_media`, replay only) are the other sources.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import re
import threading
import time
from html import unescape
from typing import Any
from urllib.parse import urlsplit

from hydra.meta import store

CDN_SUFFIXES = (".fbcdn.net", ".facebook.com", ".cdninstagram.com")
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm"}
MAX_PIXELS = 40_000_000
SNAPSHOT_MAX_BYTES = 3_000_000
_SRC_RE = re.compile(r"""(?:src|poster|content)\s*=\s*["']([^"']+)["']""", re.I)

REASON_PL = {
    "disabled": ("Automatyczne pobieranie materiałów z podglądu reklamy jest wyłączone (opcja media_fetch, domyślnie "
                 "wyłączona). Analiza obejmuje metadane i tekst; plik można dołączyć ręcznie."),
    "offline": "Tryb offline: materiałów nie pobierano. Analiza obejmuje metadane i tekst.",
    "not_live": "Dane z odtworzenia: HYDRA nie łączy się z Meta, materiały tylko z nagrania lub dołączone ręcznie.",
    "no_snapshot": "Brak adresu podglądu reklamy w odpowiedzi API.",
    "blocked": ("Podgląd reklamy wymaga JavaScriptu lub logowania albo nie zawiera bezpośrednich odnośników do "
                "plików; HYDRA nie obchodzi takich ograniczeń. Dołącz plik ręcznie."),
    "budget": "Odroczono: limit żądań Meta lub limit bajtów materiałów.",
    "host": "Odnośnik spoza hostów Meta CDN: nie pobrano.",
    "mime": "Typ pliku nieobsługiwany lub niezgodny z zawartością.",
    "corrupt": "Plik uszkodzony (dekoder nie odczytał zawartości).",
    "pixels": "Obraz przekracza limit pikseli.",
    "too_large": "Plik przekracza limit bajtów.",
    "landing_disabled": ("Pobieranie stron docelowych jest wyłączone (opcja landing_fetch, domyślnie wyłączona); "
                         "adres zapisano jako wskaźnik."),
    "landing_none": "Brak potwierdzonego adresu docelowego (podpis linku nie jest adresem docelowym).",
}


def media_enabled(cfg: dict[str, Any]) -> bool:
    """Snapshot + media download: config `media_fetch` (admin toggle) or HYDRA_META_SNAPSHOTS=1 (operator flag)."""
    import os

    return bool(cfg.get("media_fetch")) or os.environ.get("HYDRA_META_SNAPSHOTS", "").strip() == "1"


def sniff(data: bytes) -> str:
    from hydra.ingest import sniff_head

    return sniff_head(data[:8193], "")


def validate(data: bytes, *, max_bytes: int) -> tuple[str | None, str | None]:
    """(mime, None) for an acceptable image/video, (None, reason) otherwise. Content decides, never the name."""
    if len(data) > max_bytes:
        return None, "too_large"
    mime = sniff(data)
    if mime in IMAGE_MIMES:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                if im.width * im.height > MAX_PIXELS:
                    return None, "pixels"
                im.verify()
        except Exception:  # noqa: BLE001 — any decoder failure is a corrupt file
            return None, "corrupt"
        return mime, None
    if mime in VIDEO_MIMES:
        return mime, None  # duration / frame limits are enforced by hydra.media at extraction time
    return None, "mime"


def cdn_ok(url: str) -> bool:
    sp = urlsplit(url)
    h = (sp.hostname or "").lower()
    return sp.scheme == "https" and any(h.endswith(s) or h == s.lstrip(".") for s in CDN_SUFFIXES)


def snapshot_media_urls(html: str, limit: int) -> list[str]:
    """Direct image/video file links in a no-JS snapshot page (text parsing only, never executed); the caller
    accepts only Meta CDN hosts and records every other link as rejected."""
    out: list[str] = []
    for m in _SRC_RE.findall(html or ""):
        u = unescape(m).strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("http") and re.search(r"\.(jpe?g|png|webp|gif|mp4)(\?|$)", urlsplit(u).path + "?", re.I) \
                and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def _role(mime: str) -> str:
    return "ad_video" if mime.startswith("video/") else "ad_creative"


def _slot(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def acquire(settings: Any, writer: Any, *, mode: str, revision_id: int, content: dict[str, Any],
            cfg: dict[str, Any], now: float, transport: Any = None) -> dict[str, Any]:
    """Store what can be obtained for this revision; return {files: [(digest, role, mime)], status, reason}.
    Every attempt or gap is a `meta_artifacts` row with a Polish reason."""
    from hydra import netguard
    from hydra.storage import ContentStore

    cas = ContentStore(settings.cas_dir)
    files: list[tuple[str, str, str]] = []
    rows: list[dict[str, Any]] = []
    meta = content.get("meta") or {}
    max_bytes = int(cfg.get("max_media_bytes") or 0)
    limit = int(cfg.get("max_media_per_ad") or 0)

    # 1. analyst attachments (already in CAS, artifact rows written by the attach endpoint)
    for d in (content.get("creative") or {}).get("media_digests") or []:
        if cas.exists(d):
            data = cas.read_bytes(d)
            mime = sniff(data)
            files.append((d, _role(mime), mime))
    # 2. synthetic fixture media (replay only; labelled synthetic)
    for i, m in enumerate(meta.get("synthetic_media") or []):
        if mode == "live":
            break
        try:
            data = base64.b64decode(str(m.get("b64") or ""), validate=True)
        except ValueError:
            data = b""
        mime, why = validate(data, max_bytes=max_bytes or 25_000_000)
        if mime is None:
            rows.append({"role": "ad_creative", "slot": f"syn{i}", "status": "invalid_or_unsupported_media",
                         "source": "synthetic_fixture", "reason": REASON_PL[why or "mime"]})
            continue
        dg = cas.put_bytes(data)
        files.append((dg, _role(mime), mime))
        rows.append({"role": _role(mime), "slot": f"syn{i}", "status": "asset_available", "source": "synthetic_fixture",
                     "artifact_id": dg, "mime": mime, "size": len(data)})
    # 3. opt-in snapshot media (live only)
    snap = meta.get("snapshot_url_redacted")
    snap_landing: list[dict[str, str]] = []
    if mode != "live":
        if not files:
            rows.append({"role": "ad_media", "slot": "snapshot", "status": "asset_unavailable",
                         "source": "snapshot_page", "reason": REASON_PL["not_live"]})
    elif not media_enabled(cfg):
        rows.append({"role": "ad_media", "slot": "snapshot", "status": "asset_unavailable", "source": "snapshot_page",
                     "reason": REASON_PL["disabled"], "url_redacted": snap})
    elif netguard.offline(settings):
        rows.append({"role": "ad_media", "slot": "snapshot", "status": "asset_unavailable", "source": "snapshot_page",
                     "reason": REASON_PL["offline"], "url_redacted": snap})
    elif not snap:
        rows.append({"role": "ad_media", "slot": "snapshot", "status": "asset_unavailable", "source": "snapshot_page",
                     "reason": REASON_PL["no_snapshot"]})
    else:
        from contextlib import closing

        from hydra.db import connect

        with closing(connect(settings.db_path, readonly=True)) as conn:
            used = store.media_bytes(conn)
        cap = int(cfg.get("max_media_total_bytes") or 0)
        if cap and used >= cap:
            rows.append({"role": "ad_media", "slot": "snapshot", "status": "budget_deferred", "source": "snapshot_page",
                         "reason": f"Osiągnięto limit dysku na materiały Meta ({used}/{cap} B); odroczono."})
        else:
            rows += _snapshot(transport, snap, cas, files, max_bytes=min(max_bytes, max(0, cap - used)) if cap
                              else max_bytes, limit=limit, landing=snap_landing)
    # 4. landing page: pointer only unless landing_fetch (opt-in, live, online)
    landing = [u for u in (content.get("creative") or {}).get("landing_urls") or []] + snap_landing
    if not landing:
        rows.append({"role": "landing_html", "slot": "none", "status": "asset_unavailable", "source": "landing_page",
                     "reason": REASON_PL["landing_none"]})
    for u in landing[:2]:
        if mode == "live" and cfg.get("landing_fetch") and not netguard.offline(settings):
            rows.append(_landing(settings, u, cas, files, max_bytes=max_bytes))
        else:
            rows.append({"role": "landing_html", "slot": _slot(u["url"]), "status": "asset_unavailable",
                         "source": "landing_page", "url_redacted": u["url"],
                         "reason": REASON_PL["landing_disabled"] if mode == "live" else REASON_PL["not_live"]})

    def tx(c: Any) -> None:
        for r in rows:
            store.put_artifact(c, revision_id, role=r["role"], slot=r["slot"], status=r["status"], source=r["source"],
                               now=now, reason=r.get("reason"), artifact_id=r.get("artifact_id"),
                               url_redacted=r.get("url_redacted"), mime=r.get("mime"), size=r.get("size"),
                               derived=bool(r.get("derived")))
        media = [f for f in files if f[1] in ("ad_creative", "ad_video")]
        if media:
            store.set_collection(c, revision_id, "asset_available", None)
        else:
            worst = next((r for r in rows if r["status"] in ("budget_deferred", "blocked_or_login_required",
                                                             "invalid_or_unsupported_media")), None)
            st = worst["status"] if worst else "asset_unavailable"
            store.set_collection(c, revision_id, st, (worst or (rows[0] if rows else {})).get("reason"))

    writer.run(tx)
    return {"files": files, "rows": rows, "snapshot_landing": snap_landing}


SNAPSHOT_MIN_INTERVAL_S = 2.0
ROBOTS_TTL_S = 86400.0
UA = "hydra-nexus-integrations"
_THROTTLE = {"last": -1e9}
_THROTTLE_LOCK = threading.Lock()


def throttle(sleep: Any = None) -> None:
    """At most one snapshot page every SNAPSHOT_MIN_INTERVAL_S seconds per process (own politeness limit)."""
    with _THROTTLE_LOCK:
        wait = _THROTTLE["last"] + SNAPSHOT_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            (sleep or time.sleep)(wait)
        _THROTTLE["last"] = time.monotonic()


def robots_allows(transport: Any, url: str) -> tuple[bool, str]:
    """robots.txt of the snapshot host, fetched at most once a day (counted in the budget) and cached in the
    transport state; disallowed -> no snapshot request (status niedostępne with this reason)."""
    import urllib.robotparser

    from hydra.meta import transport as tp
    from hydra.meta.transport import MetaError

    sp = urlsplit(url)
    host = sp.hostname or ""
    st = tp.load_state(transport.settings)
    cache = (st.get("robots") or {}).get(host)
    if not cache or time.time() - float(cache.get("at") or 0) > ROBOTS_TTL_S:
        try:
            code, _h, body = transport.get_raw(f"https://{host}/robots.txt", with_token=False, max_bytes=1_000_000)
        except MetaError as e:
            return False, f"Nie sprawdzono robots.txt ({e.message_pl}); podglądu nie pobrano."
        cache = {"at": time.time(), "code": code, "sha256": hashlib.sha256(body).hexdigest(),
                 "body": body.decode("utf-8", "replace")[:400_000] if code == 200 else ""}
        tp.update_state(transport.settings, robots={**(st.get("robots") or {}), host: cache})
    if cache.get("code") in (401, 403):
        return False, f"robots.txt {host}: HTTP {cache['code']} (dostęp zabroniony); podglądu nie pobrano."
    if cache.get("code") != 200:
        return True, ""
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(cache["body"].splitlines())
    if rp.can_fetch(UA, url):
        return True, ""
    return False, (f"robots.txt hosta {host} nie zezwala automatom na tę ścieżkę ({sp.path}); HYDRA respektuje "
                   "robots.txt i nie pobiera podglądu. Dołącz materiał ręcznie.")


_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_NOT_DEST = ("facebook.com", "fbcdn.net", "instagram.com", "cdninstagram.com", "fb.com", "fb.me")


def snapshot_landing_urls(html: str) -> list[str]:
    """Destination links shown in the snapshot (`l.facebook.com/l.php?u=` unwrapped, other external http(s)
    links); facebook / instagram / CDN links are not destinations. Parsed as text, never followed here."""
    from urllib.parse import parse_qs

    from hydra.redact import redact_url

    out: list[str] = []
    for raw in _HREF_RE.findall(html or ""):
        u = unescape(raw).strip()
        sp = urlsplit(u)
        if (sp.hostname or "").endswith("l.facebook.com") and sp.path.startswith("/l.php"):
            u = (parse_qs(sp.query).get("u") or [""])[0]
            sp = urlsplit(u)
        h = (sp.hostname or "").lower()
        if sp.scheme not in ("http", "https") or not h or h.endswith(_NOT_DEST):
            continue
        r = redact_url(u) or ""
        if r and r not in out:
            out.append(r)
    return out[:10]


def _snapshot(transport: Any, snap: str, cas: Any, files: list, *, max_bytes: int, limit: int,
              landing: list[dict[str, str]]) -> list[dict]:
    from hydra.meta.transport import MetaError

    rows: list[dict[str, Any]] = []
    if transport is None:
        return [{"role": "ad_media", "slot": "snapshot", "status": "asset_unavailable", "source": "snapshot_page",
                 "reason": REASON_PL["offline"]}]
    base = re.sub(r"[?&]access_token=[^&]*", "", snap.replace("[REDACTED]", "")).rstrip("?&")
    allowed, why = robots_allows(transport, base)
    if not allowed:
        return [{"role": "ad_media", "slot": "snapshot", "status": "blocked_or_login_required",
                 "source": "snapshot_page", "reason": why, "url_redacted": base}]
    throttle()
    try:
        status, _h, body = transport.get_raw(base, with_token=True, max_bytes=SNAPSHOT_MAX_BYTES)
    except MetaError as e:
        st = "budget_deferred" if e.status in ("budget", "rate_limited") else "blocked_or_login_required"
        return [{"role": "ad_media", "slot": "snapshot", "status": st, "source": "snapshot_page",
                 "reason": REASON_PL["budget"] if st == "budget_deferred" else e.message_pl}]
    html = body.decode("utf-8", "replace") if status == 200 else ""
    if html:
        tok = transport.token() or ""
        clean = html.replace(tok, "[REDACTED]") if tok else html
        clean = re.sub(r"access_token=[^&\"'\s<>]*", "access_token=[REDACTED]", clean)
        data = clean.encode("utf-8")
        dg = cas.put_bytes(data)  # derived copy: the token removed before anything is stored
        rows.append({"role": "snapshot_html", "slot": "snapshot", "status": "asset_available",
                     "source": "snapshot_page", "artifact_id": dg, "mime": "text/html", "size": len(data),
                     "url_redacted": base, "derived": True,
                     "reason": "Strona podglądu reklamy (kopia pochodna: token usunięty, HTML nie jest wykonywany)."})
        for u in snapshot_landing_urls(clean):
            landing.append({"url": u, "provenance": "snapshot_link", "field": "ad_snapshot_url"})
    urls = snapshot_media_urls(html, limit)
    if not urls:
        rows.append({"role": "ad_media", "slot": "snapshot", "status": "blocked_or_login_required",
                     "source": "snapshot_page", "reason": REASON_PL["blocked"] + f" (HTTP {status})"})
        return rows
    for u in urls:
        slot = _slot(u)
        red = re.sub(r"\?.*$", "", u)
        if not cdn_ok(u):
            rows.append({"role": "ad_media", "slot": slot, "status": "invalid_or_unsupported_media",
                         "source": "snapshot_media", "reason": REASON_PL["host"], "url_redacted": red})
            continue
        try:
            st, _h2, data = transport.get_raw(u, with_token=False, max_bytes=max_bytes)
        except MetaError as e:
            stt = "budget_deferred" if e.status in ("budget", "rate_limited") else (
                "invalid_or_unsupported_media" if e.status in ("too_large", "blocked") else "asset_unavailable")
            rows.append({"role": "ad_media", "slot": slot, "status": stt, "source": "snapshot_media",
                         "reason": REASON_PL["too_large"] if e.status == "too_large" else e.message_pl,
                         "url_redacted": red})
            if stt == "budget_deferred":
                break
            continue
        mime, why = validate(data, max_bytes=max_bytes) if st == 200 else (None, "mime")
        if mime is None:
            rows.append({"role": "ad_media", "slot": slot, "status": "invalid_or_unsupported_media",
                         "source": "snapshot_media", "reason": REASON_PL[why or "mime"], "url_redacted": red})
            continue
        dg = cas.put_bytes(data)
        files.append((dg, _role(mime), mime))
        rows.append({"role": _role(mime), "slot": slot, "status": "asset_available", "source": "snapshot_media",
                     "artifact_id": dg, "mime": mime, "size": len(data), "url_redacted": red})
    return rows


def _landing(settings: Any, u: dict[str, str], cas: Any, files: list, *, max_bytes: int) -> dict[str, Any]:
    """Opt-in landing fetch through the existing isolated no-JS fetcher (SSRF guard on every hop, size caps). The
    HTML is stored as evidence and parsed by extractors as text; it is never rendered in the HYDRA origin."""
    from hydra.enrich.fetch import fetch_url

    res = fetch_url(u["url"], settings, force=True)
    final = next((h for h in reversed(res.hops) if h.body), None)
    if not res.ok or final is None:
        return {"role": "landing_html", "slot": _slot(u["url"]), "status": "asset_unavailable",
                "source": "landing_page", "url_redacted": u["url"],
                "reason": f"Strona docelowa niedostępna: {res.status}"[:300]}
    data = final.body[: max_bytes or len(final.body)]
    dg = cas.put_bytes(data)
    files.append((dg, "landing_html", "text/html"))
    return {"role": "landing_html", "slot": _slot(u["url"]), "status": "asset_available", "source": "landing_page",
            "artifact_id": dg, "mime": final.mime or "text/html", "size": len(data), "url_redacted": u["url"],
            "derived": False}


def attach(settings: Any, writer: Any, *, mode: str, ad_id: str, data: bytes, filename: str, now: float,
           max_bytes: int) -> tuple[int, str]:
    """Analyst attachment: validated by content, stored in CAS, a new revision with an artifact row."""
    from hydra.storage import ContentStore

    mime, why = validate(data, max_bytes=max_bytes)
    if mime is None:
        raise ValueError(REASON_PL[why or "mime"])
    dg = ContentStore(settings.cas_dir).put_bytes(data)

    def tx(c: Any) -> int:
        rid, _new = store.attach_revision(c, mode, ad_id, dg, now)
        store.put_artifact(c, rid, role=_role(mime), slot=dg[:12], status="asset_available", source="analyst_upload",
                           now=now, artifact_id=dg, mime=mime, size=len(data),
                           reason=f"Dołączone przez analityka ({filename[:80]}).")
        return rid

    return writer.run(tx), dg


def b64_png(color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (64, 48)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    with contextlib.suppress(Exception):
        Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()
