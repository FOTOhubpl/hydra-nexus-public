"""Isolated live fetch of a URL given as input (GUI "analyse a URL"), enabled only with Settings.enrich.

    res = fetch_url("https://example.org/landing?sub1=x")
    res.har_path   # minimal HAR-like JSON: the har/dom extractors run on it unchanged
    res.record()   # EnrichmentRecord (source "http_fetch") for persistence

Rules: no JavaScript, GET only, at most 10 redirects (HTTP 3xx and `<meta http-equiv=refresh>`), 15 s timeout
per request, 5 MB cap for HTML and 512 kB for anything else, a realistic browser User-Agent, no cookies kept
between runs. Only public (global) IP addresses are contacted (SSRF guard). Records per hop: URL, status,
headers, server IP from the socket, TLS certificate (subject, issuer, SANs, validity) and verification result.
The final HTML and the favicon are stored in the HAR-like file. JS redirects are not followed; the parser
reports them as `js_redirect` genes. Cloaking is expected: a bot often receives a white page, so the
response is scored for white-page suspicion (a gene), never treated as a failure.

Implemented with the standard library (http.client + ssl) because httpx is a dev-only dependency in uv.lock
and the stdlib socket gives the peer IP and the raw certificate directly.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from hydra.config import Settings
from hydra.contracts import EnrichmentRecord, GeneStatus
from hydra.enrich.client import default_cache_dir, live_enabled, now_iso, record_key, safe_name
from hydra.netguard import ALLOWED_SCHEMES, is_local_name, is_public_ip, literal_ip, offline

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Connection": "close",
}
MAX_REDIRECTS = 10
TIMEOUT = 15.0
MAX_HTML = 5_000_000
MAX_OTHER = 512_000
META_REFRESH = re.compile(
    rb"<meta[^>]+http-equiv=['\"]?refresh['\"]?[^>]*content=['\"]?\s*\d+\s*;\s*url=([^'\">\s]+)", re.I
)


HOP_DEADLINE = 30.0  # seconds per hop (connect + TLS + headers + body), monotonic
TOTAL_DEADLINE = 90.0  # seconds for the whole chain incl. favicon
WHITE_PAGE_SCAN = 500_000  # bytes of hostile HTML scanned with regexes (avoids pathological backtracking)


class FetchBlocked(Exception):
    pass


class DeadlineExceeded(OSError):
    """A hop or the whole fetch ran past its monotonic deadline (drip-feeding server)."""


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExceeded("fetch deadline exceeded")
    return min(left, TIMEOUT)


@dataclass
class Hop:
    url: str
    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    ip: str | None = None
    tls: dict[str, Any] | None = None
    redirect_to: str | None = None
    kind: str | None = None  # http3xx | meta_refresh
    started: str = ""
    mime: str = ""
    body: bytes = b""
    truncated: bool = False
    error: str | None = None


@dataclass
class FetchResult:
    url: str
    hops: list[Hop]
    final_url: str | None
    status: str  # ok | error:<reason> | disabled | blocked:<reason>
    white_page: list[str] = field(default_factory=list)
    favicon: bytes | None = None
    fetched_at: str = field(default_factory=now_iso)
    har_path: Path | None = None
    meta_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def record(self) -> EnrichmentRecord:
        sha = (
            hashlib.sha256(self.har_path.read_bytes()).hexdigest()
            if self.har_path and self.har_path.is_file()
            else None
        )
        return EnrichmentRecord(
            key=record_key("http_fetch", self.url),
            source="http_fetch",
            query=self.url,
            fetched_at=self.fetched_at,
            status=GeneStatus.OBSERVED if self.ok else GeneStatus.UNAVAILABLE,
            response_sha256=sha,
            cache_path=str(self.har_path) if self.har_path else None,
            summary={
                "status": self.status,
                "final_url": self.final_url,
                "hops": len(self.hops),
                "chain": [(h.status, h.url[:200]) for h in self.hops],
                "white_page": self.white_page,
                "server_ips": sorted({h.ip for h in self.hops if h.ip}),
            },
        )


def _public_ip(host: str, port: int) -> str:
    """The vetted IP to connect to. Literal IPs in any notation (`2130706433`, `0177.0.0.1`, IPv6) and local names
    are judged without DNS; otherwise resolve once and require every address to be public
    (hydra.netguard.is_public_ip). The caller connects to the returned IP: no second (rebinding) resolution."""
    ip_literal = literal_ip(host)
    if ip_literal is not None:
        if not is_public_ip(ip_literal):
            raise FetchBlocked(f"non-public address {ip_literal}")
        return str(ip_literal)
    if is_local_name(host):
        raise FetchBlocked(f"local host name {host!r}")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = [addr[0] for _fam, _, _, _, addr in infos]
    for ip in ips:
        if not is_public_ip(ip):
            raise FetchBlocked(f"non-public address {ip}")
    if not ips:
        raise FetchBlocked("no address")
    return ips[0]


def _cert_info(der: bytes | None, verified: bool, verify_error: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"verified": verified, "verify_error": verify_error}
    if not der:
        return out
    out["sha256"] = hashlib.sha256(der).hexdigest()
    try:
        from cryptography import x509

        c = x509.load_der_x509_certificate(der)
        out["subject"] = c.subject.rfc4514_string()
        out["issuer"] = c.issuer.rfc4514_string()
        out["not_before"] = c.not_valid_before_utc.isoformat()
        out["not_after"] = c.not_valid_after_utc.isoformat()
        try:
            ext = c.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            out["sans"] = sorted(ext.value.get_values_for_type(x509.DNSName))[:200]
        except x509.ExtensionNotFound:
            out["sans"] = []
    except Exception as exc:  # noqa: BLE001 — malformed certificates are evidence, not a crash
        out["parse_error"] = type(exc).__name__
    return out


def _one(url: str, deadline: float | None = None) -> Hop:
    dl = min(deadline or float("inf"), time.monotonic() + HOP_DEADLINE)
    hop = Hop(url=url, started=datetime.now(UTC).isoformat(timespec="milliseconds"))
    try:
        sp = urlsplit(url)
        port = sp.port or (443 if sp.scheme == "https" else 80)
    except ValueError as exc:
        raise FetchBlocked(f"malformed URL: {exc}") from exc
    if sp.scheme not in ALLOWED_SCHEMES or not sp.hostname:
        raise FetchBlocked(f"unsupported URL scheme {sp.scheme!r}")
    ip = _public_ip(sp.hostname, port)
    hop.ip = ip
    raw = socket.create_connection((ip, port), timeout=_remaining(dl))
    sock: socket.socket = raw
    try:
        if sp.scheme == "https":
            verified, verr = True, None
            ctx = ssl.create_default_context()
            try:
                sock = ctx.wrap_socket(raw, server_hostname=sp.hostname)
            except ssl.SSLError as e:
                raw.close()
                verified, verr = False, f"{type(e).__name__}: {e}"[:200]
                raw = socket.create_connection((ip, port), timeout=_remaining(dl))
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(raw, server_hostname=sp.hostname)
            hop.tls = _cert_info(sock.getpeercert(binary_form=True), verified, verr)  # type: ignore[union-attr]
        sock.settimeout(_remaining(dl))
        conn = http.client.HTTPConnection(sp.hostname, port, timeout=_remaining(dl))
        conn.sock = sock  # pre-connected to the vetted IP: no second DNS resolution (rebinding guard)
        path = (sp.path or "/") + (f"?{sp.query}" if sp.query else "")
        host_hdr = sp.hostname + (f":{sp.port}" if sp.port else "")
        conn.request("GET", path, headers={**HEADERS, "Host": host_hdr})
        r = conn.getresponse()
        hop.status = r.status
        hop.headers = [(k, v) for k, v in r.getheaders()]
        hop.mime = (r.getheader("content-type") or "").split(";")[0].strip().lower()
        cap = MAX_HTML if hop.mime in ("text/html", "application/xhtml+xml", "") else MAX_OTHER
        chunks: list[bytes] = []
        got = 0
        while got <= cap:  # read1: returns what has arrived, so a drip server hits the deadline, not a hang
            left = _remaining(dl)
            if sock.fileno() != -1:  # http.client closes the socket once a sized body is fully read
                sock.settimeout(left)
            try:
                part = r.read1(65536)
            except TimeoutError as e:
                if time.monotonic() >= dl - 0.05:
                    raise DeadlineExceeded("fetch deadline exceeded") from e
                raise
            if not part:
                break
            chunks.append(part)
            got += len(part)
        hop.body = b"".join(chunks)
        if len(hop.body) > cap:
            hop.body, hop.truncated = hop.body[:cap], True
        loc = r.getheader("location")
        if 300 <= r.status < 400 and loc:
            hop.redirect_to, hop.kind = urljoin(url, loc), f"http{r.status}"
        elif hop.mime in ("text/html", "") and (m := META_REFRESH.search(hop.body[:200_000])):
            hop.redirect_to = urljoin(url, m.group(1).decode("utf-8", "replace").strip("'\""))
            hop.kind = "meta_refresh"
        conn.close()
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return hop


def _strip_markup(low: str) -> str:
    """Linear-time removal of <script>/<style> blocks and tags (no backtracking regex on hostile HTML)."""
    out: list[str] = []
    i, n = 0, len(low)
    while i < n:
        j = low.find("<", i)
        if j < 0:
            out.append(low[i:])
            break
        out.append(low[i:j])
        for tag in ("script", "style"):
            if low.startswith("<" + tag, j):
                end = low.find("</" + tag, j)
                j = n if end < 0 else end
                break
        k = low.find(">", j)
        nxt = low.find("<", j + 1)
        if k < 0:
            out.append(low[j:])
            break
        if 0 <= nxt < k and not low.startswith(("<script", "<style"), j):
            out.append(low[j:nxt])  # an unclosed '<' is text: resume at the next '<'
            i = nxt
            continue
        i = k + 1
        out.append(" ")
    return "".join(out)


def white_page_signals(hop: Hop, entry_url: str) -> list[str]:
    """Heuristic signs that the fetcher got a cloaked/white page (bots are often served a decoy)."""
    out: list[str] = []
    body = hop.body[:WHITE_PAGE_SCAN].decode("utf-8", "replace")
    low = body.lower()
    text = _strip_markup(low)
    words = len(re.findall(r"\w{3,}", text))
    if hop.status and hop.status >= 400:
        out.append(f"http_{hop.status}")
    if "just a moment" in low or "cf-chl" in low or "challenge-platform" in low:
        out.append("bot_challenge")
    if words < 80:
        out.append("tiny_text")
    if "<form" not in low and "<script" not in low:
        out.append("no_forms_no_scripts")
    if (
        re.search(
            r"privacy policy|terms of (use|service)|lorem ipsum|coming soon|under construction|"
            r"domain (is )?for sale|parked",
            low,
        )
        and words < 600
    ):
        out.append("decoy_text")
    eh, fh = urlsplit(entry_url).hostname or "", urlsplit(hop.url).hostname or ""
    if fh and eh and fh != eh and re.search(r"(^|\.)(google|bing|yahoo|wikipedia|youtube|facebook)\.", fh):
        out.append("redirect_to_mainstream")
    return out


def fetch_url(
    url: str, settings: Settings | None = None, *, force: bool = False, out_dir: Path | None = None
) -> FetchResult:
    """Fetch `url` once (if Settings.enrich or force), store a HAR-like evidence file, return the result.

    When live lookups are disabled, a cached previous fetch of the same URL is replayed if present."""
    s = settings or Settings()
    base = out_dir or default_cache_dir(s) / "http_fetch"
    stem = safe_name(url)[:100] + "_" + hashlib.sha256(url.encode()).hexdigest()[:12]
    har_path, meta_path = base / f"{stem}.har", base / f"{stem}.json"
    # HYDRA_OFFLINE=1 dominates `force` (CR-ASTRA-SEC-8): offline only replays a previous fetch.
    if offline(s) or not (force or live_enabled(s)):
        if meta_path.is_file():
            j = json.loads(meta_path.read_text())
            return FetchResult(
                url,
                [],
                j.get("final_url"),
                j.get("status", "ok"),
                j.get("white_page", []),
                fetched_at=j.get("fetched_at", now_iso()),
                har_path=har_path if har_path.is_file() else None,
                meta_path=meta_path,
            )
        return FetchResult(url, [], None, "disabled")
    hops: list[Hop] = []
    status = "ok"
    cur: str | None = url
    seen: set[str] = set()
    deadline = time.monotonic() + TOTAL_DEADLINE
    try:
        while cur and len(hops) <= MAX_REDIRECTS:
            if cur in seen:
                status = "error:redirect_loop"
                break
            seen.add(cur)
            hop = _one(cur, deadline)
            hops.append(hop)
            cur = hop.redirect_to
        else:
            if cur:
                status = "error:too_many_redirects"
    except FetchBlocked as e:
        status = f"blocked:{e}"
    except (OSError, http.client.HTTPException, ssl.SSLError) as e:
        status = f"error:{type(e).__name__}"
        if hops:
            hops[-1].error = status
    final = hops[-1] if hops else None
    res = FetchResult(url, hops, final.url if final else None, status)
    if final is not None and final.status:
        res.white_page = white_page_signals(final, url)
        res.favicon = _favicon(final, deadline)
    base.mkdir(parents=True, exist_ok=True)
    har_path.write_text(json.dumps(_to_har(res), ensure_ascii=False))
    res.har_path = har_path
    meta_path.write_text(
        json.dumps(
            {
                "url": url,
                "final_url": res.final_url,
                "status": status,
                "white_page": res.white_page,
                "fetched_at": res.fetched_at,
                "hops": [(h.status, h.url, h.ip) for h in hops],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    res.meta_path = meta_path
    return res


def _favicon(final: Hop, deadline: float | None = None) -> bytes | None:
    m = re.search(rb"<link[^>]+rel=['\"]?[^'\">]*icon[^>]*>", final.body[:300_000], re.I)
    href = None
    if m:
        h = re.search(rb"href=['\"]?([^'\" >]+)", m.group(0), re.I)
        href = h.group(1).decode("utf-8", "replace") if h else None
    target = urljoin(final.url, href or "/favicon.ico")
    if href and href.startswith("data:"):
        try:
            return base64.b64decode(href.split(",", 1)[1])
        except (ValueError, IndexError):
            return None
    try:
        hop = _one(target, deadline)
    except (FetchBlocked, OSError, http.client.HTTPException, ssl.SSLError):
        return None
    if hop.status == 200 and hop.body and not hop.mime.startswith("text/"):
        final_fav = hop.body[:MAX_OTHER]
        final.__dict__.setdefault("_favicon_url", target)
        return final_fav
    return None


def _to_har(res: FetchResult) -> dict[str, Any]:
    entries = []
    for h in res.hops:
        text, enc = None, None
        if h.body:
            if h.mime.startswith("text/") or h.mime in ("", "application/xhtml+xml", "application/json"):
                text = h.body.decode("utf-8", "replace")
            else:
                text, enc = base64.b64encode(h.body).decode(), "base64"
        content: dict[str, Any] = {"mimeType": h.mime or "text/html", "size": len(h.body)}
        if text is not None:
            content["text"] = text
        if enc:
            content["encoding"] = enc
        entries.append(
            {
                "startedDateTime": h.started,
                "time": 0,
                "_resourceType": "document",
                "serverIPAddress": h.ip,
                "request": {
                    "method": "GET",
                    "url": h.url,
                    "headers": [{"name": k, "value": v} for k, v in HEADERS.items()],
                },
                "response": {
                    "status": h.status or 0,
                    "headers": [{"name": k, "value": v} for k, v in h.headers],
                    "redirectURL": h.redirect_to if h.kind and h.kind.startswith("http") else "",
                    "content": content,
                    "_redirect_kind": h.kind,
                    "_redirect_to": h.redirect_to,
                    "_truncated": h.truncated,
                },
                "_tls": h.tls,
                "_error": h.error,
            }
        )
    if res.favicon and res.hops:
        entries.append(
            {
                "startedDateTime": res.hops[-1].started,
                "time": 0,
                "_resourceType": "image",
                "serverIPAddress": res.hops[-1].ip,
                "request": {"method": "GET", "url": urljoin(res.hops[-1].url, "/favicon.ico"), "headers": []},
                "response": {
                    "status": 200,
                    "headers": [],
                    "content": {
                        "mimeType": "image/x-icon",
                        "size": len(res.favicon),
                        "text": base64.b64encode(res.favicon).decode(),
                        "encoding": "base64",
                    },
                },
            }
        )
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "hydra-fetch", "version": "1"},
            "pages": [{"title": res.url, "startedDateTime": res.fetched_at}],
            "entries": entries,
            "_hydra_fetch": {
                "status": res.status,
                "final_url": res.final_url,
                "white_page": res.white_page,
                "fetched_at": res.fetched_at,
            },
        }
    }
