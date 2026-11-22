"""The only outbound HTTP path for integrations.

`request()` fails closed, in this order, before any byte leaves the machine:
1. `HYDRA_OFFLINE=1` -> `IntegrationError("offline")` with a Polish message (never a silent fallback);
2. connector disabled -> `"disabled"` (the explicit Test button passes `require_enabled=False`);
3. `netguard.check_url(url, resolve=True)`: http/https only, every resolved address public (loopback only when
   the caller allows it, e.g. a local test sink) -> `"blocked"`;
4. per-connector rate limit (`Connector.rate_limit_per_min`, rolling 60 s) -> `"rate_limited"`.
Then the transport runs with a timeout and a response size cap. Every attempted request (also failed ones) is
audited as `INTEGRATION_REQUEST` {connector, method, host, path (no query), status, ms, bytes_out, bytes_in,
payload_sha256} through the single Writer. Secret values are scrubbed from every message and exception text.

Tests inject a transport with `set_transport()`; no real network is used in tests.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from hydra import events, netguard
from hydra.integrations import state
from hydra.redact import redact_text

USER_AGENT = "hydra-nexus-integrations/1.0"
DEFAULT_TIMEOUT_S = 20.0
MAX_RESPONSE_BYTES = 5_000_000
EVENT = "INTEGRATION_REQUEST"
MASK = "••••"

MESSAGE_OFFLINE_PL = (
    "Tryb offline (HYDRA_OFFLINE=1): połączenia z usługami zewnętrznymi są zablokowane. Aby połączyć się z "
    "dostawcą, uruchom HYDRA z HYDRA_OFFLINE=0 (dla istniejących modułów wzbogacania także HYDRA_ENRICH=1), "
    "włącz konektor i kliknij Testuj. Eksport do plików działa offline."
)
MESSAGE_DISABLED_PL = "Konektor jest wyłączony. Włącz go, aby wysyłać zapytania."
MESSAGE_RATE_PL = "Przekroczono limit zapytań konektora ({limit}/min). Spróbuj za chwilę."

# (method, url, headers, body, timeout_s, max_bytes) -> (status_code, response_headers, body)
Transport = Callable[[str, str, dict[str, str], bytes | None, float, int], tuple[int, dict[str, str], bytes]]


class IntegrationError(Exception):
    """Safe to show: `status` is a machine id, `message_pl` never contains a secret."""

    def __init__(self, status: str, message_pl: str, http_status: int | None = None) -> None:
        super().__init__(message_pl)
        self.status = status
        self.message_pl = message_pl
        self.http_status = http_status


@dataclass
class HttpResult:
    status_code: int
    headers: dict[str, str]
    body: bytes
    ms: int
    url_redacted: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float,
                      max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # type: ignore[no-untyped-def]
            return None  # redirects are not followed: the vetted destination is the only one

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:  # noqa: S310 — URL vetted by netguard.check_url
            data = r.read(max_bytes + 1)
            return r.status, {k.lower(): v for k, v in r.headers.items()}, data
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read(min(max_bytes, 200_000))


_transport: Transport = _urllib_transport


def set_transport(fn: Transport | None) -> Transport:
    """Replace the transport (tests). Returns the previous one; `None` restores urllib."""
    global _transport
    prev = _transport
    _transport = fn or _urllib_transport
    return prev


def scrub(text: Any, secrets: Iterable[str | None] = ()) -> str:
    s = str(text)
    parts: list[str] = []
    for v in secrets:
        if not v or len(v) < 4:
            continue
        parts.append(v)
        if v.lower().startswith(("http://", "https://")):  # a URL secret (Slack webhook): its path is the secret
            try:
                p = urlsplit(v)
            except ValueError:
                continue
            if len(p.path) > 1:
                parts.append(p.path)
            if p.query:
                parts.append(p.query)
    for v in sorted(parts, key=len, reverse=True):
        s = s.replace(v, MASK)
    return redact_text(s) or ""


def redacted_url(url: str) -> str:
    """scheme://host/path without query string or user info (audit, preview, cache)."""
    sp = urlsplit(url)
    host = sp.hostname or ""
    port = f":{sp.port}" if sp.port else ""
    return f"{sp.scheme}://{host}{port}{sp.path or '/'}"


def _is_loopback(host: str) -> bool:
    h = host.strip("[]").lower()
    if h in ("localhost", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _audit(writer: Any, payload: dict[str, Any]) -> None:
    if writer is None:
        return
    try:
        writer.run(lambda conn: events.append(conn, EVENT, payload), timeout=30)
    except Exception:  # noqa: BLE001 — auditing must not turn a provider answer into a crash
        pass


def request(connector_id: str, method: str, url: str, *, headers: dict[str, str] | None = None,
            body: bytes | None = None, settings: Any, writer: Any, secrets: Iterable[str | None] = (),
            require_enabled: bool = True, allow_loopback: bool = False, timeout: float = DEFAULT_TIMEOUT_S,
            max_bytes: int = MAX_RESPONSE_BYTES) -> HttpResult:
    from hydra.integrations import registry

    secrets = [s for s in secrets if s]
    conn = registry.get(connector_id)
    if conn is None:
        raise IntegrationError("error", "Nieznany konektor.")
    method = method.upper()
    sp = urlsplit(url)
    host = (sp.hostname or "").lower()
    path = scrub(sp.path or "/", secrets)
    audit: dict[str, Any] = {"connector": connector_id, "method": method, "host": host, "path": path,
                             "status": None, "ms": 0, "bytes_out": len(body or b""), "bytes_in": 0,
                             "payload_sha256": hashlib.sha256(body or b"").hexdigest()}
    if netguard.offline(settings):
        raise IntegrationError("offline", MESSAGE_OFFLINE_PL)  # nothing attempted: no audit row needed
    if require_enabled and not state.is_enabled(settings, connector_id):
        raise IntegrationError("disabled", MESSAGE_DISABLED_PL)
    try:
        if not (allow_loopback and _is_loopback(host) and sp.scheme in ("http", "https")):
            netguard.check_url(url, resolve=True)
    except netguard.UnsafeDestination as exc:
        audit["status"] = "blocked"
        _audit(writer, audit)
        raise IntegrationError("blocked", "Adres docelowy odrzucony przez politykę sieci (dozwolone tylko "
                                          f"publiczne adresy http/https): {scrub(exc, secrets)}") from None
    limit = conn.rate_limit_per_min
    if limit and state.requests_since(settings, connector_id, 60.0) >= limit:
        audit["status"] = "rate_limited"
        _audit(writer, audit)
        raise IntegrationError("rate_limited", MESSAGE_RATE_PL.format(limit=int(limit) if limit >= 1 else limit))
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    state.note_request(settings, connector_id)
    t0 = time.monotonic()
    try:
        code, rh, data = _transport(method, url, hdrs, body, timeout, max_bytes)
    except Exception as exc:  # noqa: BLE001 — every transport failure becomes a scrubbed, audited error
        audit.update(status=f"error:{type(exc).__name__}", ms=int((time.monotonic() - t0) * 1000))
        _audit(writer, audit)
        raise IntegrationError("error", f"Błąd połączenia z {host}: {type(exc).__name__}: "
                                        f"{scrub(exc, secrets)[:200]}") from None
    ms = int((time.monotonic() - t0) * 1000)
    audit.update(status=int(code), ms=ms, bytes_in=len(data))
    _audit(writer, audit)
    if len(data) > max_bytes:
        raise IntegrationError("error", f"Odpowiedź z {host} przekracza limit {max_bytes} bajtów.")
    return HttpResult(int(code), dict(rh or {}), data, ms, redacted_url(url))
