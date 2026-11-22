"""The only Meta Ad Library HTTP path (collector, diagnostics, poll-now, scheduler delegation, legacy adlibrary).

Fail closed, in this order, before a request is made:
1. `HYDRA_OFFLINE=1` -> MetaError("offline") (no fallback);
2. no token -> MetaError("needs_config");
3. persisted provider back-off (after 429 / Graph throttling codes, `Retry-After`, BUC
   `estimated_time_to_regain_access`) -> MetaError("rate_limited") without a request;
4. HYDRA's own hourly budget (default 60 HTTP requests / rolling hour to Meta, every purpose counted: collection,
   diagnostics, retries, poll-now, snapshot and media fetches) -> MetaError("budget") = "paused until resumed",
   never "no ads". The counter (`<state>/meta/transport.json`, every request noted before it is sent) is shared by
   every process of the deployment that uses the same state dir; every Meta path goes through this class.
Then the request goes through `hydra.integrations.http.request` (connector enabled flag, netguard on every resolved
address, per-minute limit, audited `INTEGRATION_REQUEST` with path only: the token is a query parameter and never
reaches the audit row) or, under WATCH-RT, through the engine's injected fetch (the same http.request in live mode).

There are no inline retry loops: a failed page keeps its cursor and is retried in a later cycle (bounded exponential
back-off with jitter for throttling). The budget is our own limit, not a Meta quota (S3 documents no universal
number); Meta's answers always take precedence.
"""

from __future__ import annotations

import contextlib
import json
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from hydra import netguard
from hydra.meta import contract

CID = "meta-adlibrary"
DEFAULT_BUDGET_PER_H = 60
WINDOW_S = 3600.0
BACKOFF_BASE_S = 300.0
BACKOFF_MAX_S = 3600.0
MAX_BYTES = 20_000_000
_LOCK = threading.RLock()

MSG_PL = {
    "offline": ("Tryb offline (HYDRA_OFFLINE=1): HYDRA nie łączy się z Meta. Kolektor nie korzysta z danych "
                "zastępczych."),
    "needs_config": ("Brak tokenu Meta Ad Library (META_AD_LIBRARY_TOKEN). Zapisz go w Integracje → Meta Ad Library → "
                     "sekret albo w pliku konektora (0600); token nigdy nie trafia do bazy, logów ani nagrań."),
    "token_invalid": ("Meta odrzuciła token (wygasły lub nieważny). Kolektor wstrzymany do czasu zapisania nowego "
                      "tokenu; brak ponowień."),
    "permission": ("Brak uprawnień do Ad Library API (np. niepotwierdzona tożsamość lub niezaakceptowane warunki). "
                   "Kolektor wstrzymany do zmiany konfiguracji."),
    "budget": ("Wykorzystano własny budżet HYDRY ({used}/{limit} żądań w ostatniej godzinie). Wstrzymano do "
               "wznowienia; to nie oznacza braku reklam."),
    "rate_limited": "Meta ograniczyła liczbę zapytań (kod {code}). Odroczono do {until}.",
    "params": "Meta odrzuciła parametry zapytania (kod {code}): {msg}",
    "transient": "Przejściowy błąd Meta (HTTP {http}, kod {code}). Strona zostanie ponowiona w kolejnym obiegu.",
    "error": "Błąd połączenia z Meta: {msg}",
    "disabled": "Konektor Meta Ad Library jest wyłączony.",
    "blocked": "Adres odrzucony przez politykę sieci.",
}
CONFIG_STATES = frozenset({"needs_config", "token_invalid", "permission"})


class MetaError(Exception):
    """Safe to show: `message_pl` never contains the token."""

    def __init__(self, status: str, message_pl: str, *, http_status: int | None = None, code: Any = None,
                 retry_at: float | None = None, usage: dict[str, Any] | None = None) -> None:
        super().__init__(message_pl)
        self.status, self.message_pl, self.http_status, self.code = status, message_pl, http_status, code
        self.retry_at, self.usage = retry_at, usage


@dataclass
class MetaResponse:
    status: int
    data: dict[str, Any]
    usage: dict[str, Any] | None
    ms: int
    requests: int = 1
    url_redacted: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# WATCH-RT fetch shape: (connector_id, method, url, headers) -> Resp(status, headers, body, ms)
Fetch = Callable[[str, str, str, dict[str, str]], Any]


def _state_file(settings: Any) -> Path:
    d = Path(settings.state_dir) / "meta"
    d.mkdir(parents=True, exist_ok=True)
    return d / "transport.json"


def load_state(settings: Any) -> dict[str, Any]:
    try:
        v = json.loads(_state_file(settings).read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(settings: Any, st: dict[str, Any]) -> None:
    p = _state_file(settings)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def update_state(settings: Any, **kv: Any) -> dict[str, Any]:
    with _LOCK:
        st = load_state(settings)
        st.update(kv)
        _save_state(settings, st)
        return st


def _requests(st: dict[str, Any], now: float) -> list[float]:
    return sorted(t for t in st.get("requests") or [] if isinstance(t, int | float) and now - t < WINDOW_S)


def note_request(settings: Any, now: float) -> None:
    """Count one HTTP request to Meta in HYDRA's own rolling-hour budget (every purpose, before it is sent)."""
    with _LOCK:
        st = load_state(settings)
        st["requests"] = _requests(st, now) + [now]
        _save_state(settings, st)


def used_last_hour(settings: Any, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return len(_requests(load_state(settings), now))


def budget_view(settings: Any, limit: int, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    st = load_state(settings)
    reqs = _requests(st, now)
    used = len(reqs)
    deferred = None
    if used >= limit and reqs:
        deferred = reqs[max(0, used - limit)] + WINDOW_S
    bo = st.get("backoff_until")
    if bo and bo > now:
        deferred = max(deferred or 0, bo)
    return {"limit_per_h": limit, "used_60m": used, "remaining": max(0, limit - used),
            "label_pl": "Własny limit HYDRY (propozycja pilotażowa), nie limit Meta; ograniczenia Meta mają "
                        "pierwszeństwo.",
            "deferred_until": _iso(deferred) if deferred else None, "last_usage": st.get("last_usage")}


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _usage_headers(headers: dict[str, str]) -> dict[str, Any] | None:
    """X-App-Usage / X-Business-Use-Case-Usage / X-Ad-Account-Usage as parsed JSON (numbers and type names only).
    None = the response carried no usage header (that means "no information", never "zero usage")."""
    out: dict[str, Any] = {}
    for k in ("x-app-usage", "x-business-use-case-usage", "x-ad-account-usage"):
        raw = headers.get(k)
        if not raw:
            continue
        try:
            v = json.loads(raw)
        except ValueError:
            continue
        out[k] = _numbers_only(v)
    return out or None


def _numbers_only(v: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(v, dict):
        return {str(k)[:40]: _numbers_only(x, depth + 1) for k, x in list(v.items())[:20]
                if isinstance(x, int | float | dict | list) or k == "type"}
    if isinstance(v, list):
        return [_numbers_only(x, depth + 1) for x in v[:10]]
    if isinstance(v, int | float):
        return v
    return str(v)[:40] if isinstance(v, str) else None


def _regain_s(usage: dict[str, Any] | None) -> float | None:
    """Largest `estimated_time_to_regain_access` (minutes) in a BUC usage header."""
    best = None
    buc = (usage or {}).get("x-business-use-case-usage")
    if isinstance(buc, dict):
        for rows in buc.values():
            for r in rows if isinstance(rows, list) else []:
                m = r.get("estimated_time_to_regain_access") if isinstance(r, dict) else None
                if isinstance(m, int | float) and m > 0:
                    best = max(best or 0.0, float(m) * 60.0)
    return best


def scrub(obj: Any, token: str | None) -> Any:
    from hydra.enrich.adlibrary import _scrub

    return _scrub(obj, token)


class MetaTransport:
    def __init__(self, settings: Any, writer: Any, *, fetch: Fetch | None = None, budget_per_h: int | None = None,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None,
                 require_enabled: bool = True) -> None:
        self.settings, self.writer, self.fetch = settings, writer, fetch
        self.budget_per_h = int(budget_per_h or DEFAULT_BUDGET_PER_H)
        self.clock, self.rng = clock, rng or random.Random()
        self.require_enabled = require_enabled
        self.requests = 0

    # ------------------------------------------------------------------ gates
    def token(self) -> str | None:
        from hydra.integrations.secrets import meta_token

        return meta_token()

    def preflight(self) -> str:
        """Raise MetaError when no request may be made now; return the token otherwise."""
        if netguard.offline(self.settings):
            raise MetaError("offline", MSG_PL["offline"])
        tok = self.token()
        if not tok:
            raise MetaError("needs_config", MSG_PL["needs_config"])
        now = self.clock()
        st = load_state(self.settings)
        if st.get("token_rejected") and st.get("token_fp") == _fp(tok):
            raise MetaError(st.get("token_rejected_status") or "token_invalid",
                            MSG_PL.get(st.get("token_rejected_status") or "token_invalid", MSG_PL["token_invalid"]))
        bo = st.get("backoff_until")
        if bo and bo > now:
            raise MetaError("rate_limited", MSG_PL["rate_limited"].format(code=st.get("backoff_code"),
                                                                          until=_iso(bo)), retry_at=bo)
        used = used_last_hour(self.settings, now)
        if used >= self.budget_per_h:
            v = budget_view(self.settings, self.budget_per_h, now)
            raise MetaError("budget", MSG_PL["budget"].format(used=used, limit=self.budget_per_h),
                            retry_at=_parse_iso(v["deferred_until"]))
        return tok

    # ------------------------------------------------------------------ request
    def get(self, params: dict[str, str], *, url: str | None = None) -> MetaResponse:
        tok = self.preflight()
        base = url or contract.archive_url()
        full = base + "?" + urlencode({**params, "access_token": tok})
        t0 = time.monotonic()
        self.requests += 1
        note_request(self.settings, self.clock())
        status, headers, body = self._send(full, tok)
        ms = int((time.monotonic() - t0) * 1000)
        usage = _usage_headers(headers)
        try:
            data = json.loads(body or b"null")
        except ValueError:
            data = None
        data = scrub(data, tok)
        err = data.get("error") if isinstance(data, dict) else None
        err = err if isinstance(err, dict) else {}
        upd: dict[str, Any] = {"last_request_at": _iso(self.clock())}
        if usage is not None:
            upd["last_usage"] = usage
        if status == 200 and not err and isinstance(data, dict):
            upd.update(backoff_n=0)
            update_state(self.settings, **upd)
            return MetaResponse(status, data, usage, ms, url_redacted=base)
        code = err.get("code")
        msg = str(scrub(str(err.get("message") or ""), tok))[:200]
        from hydra.redact import redact_text

        msg = redact_text(msg) or ""
        if status == 429 or code in contract.THROTTLE_CODES:
            st = load_state(self.settings)
            n = int(st.get("backoff_n") or 0) + 1
            wait = _retry_after(headers) or _regain_s(usage)
            if wait is None:
                wait = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** (n - 1)))
                wait *= 1.0 + self.rng.uniform(0.0, 0.2)
            until = self.clock() + min(wait, 6 * BACKOFF_MAX_S)
            update_state(self.settings, **upd, backoff_until=until, backoff_n=n, backoff_code=code or status)
            raise MetaError("rate_limited", MSG_PL["rate_limited"].format(code=code or status, until=_iso(until)),
                            http_status=status, code=code, retry_at=until, usage=usage)
        if status == 401 or code in contract.TOKEN_CODES:
            update_state(self.settings, **upd, token_rejected=True, token_rejected_status="token_invalid",
                         token_fp=_fp(tok))
            raise MetaError("token_invalid", MSG_PL["token_invalid"], http_status=status, code=code)
        if status == 403 or code in contract.PERMISSION_CODES:
            update_state(self.settings, **upd, token_rejected=True, token_rejected_status="permission",
                         token_fp=_fp(tok))
            raise MetaError("permission", MSG_PL["permission"] + (f" ({msg})" if msg else ""), http_status=status,
                            code=code)
        update_state(self.settings, **upd)
        if status == 400 or code in contract.PARAM_CODES:
            raise MetaError("params", MSG_PL["params"].format(code=code, msg=msg), http_status=status, code=code)
        raise MetaError("transient", MSG_PL["transient"].format(http=status, code=code), http_status=status, code=code)

    def get_raw(self, url: str, *, with_token: bool, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
        """Snapshot page (with_token: the render URL needs the token) or a media file on the Meta CDN (never with
        the token). Same gates and budget as `get`; no redirects are followed (http.request), every address is
        checked by netguard."""
        tok = self.preflight()
        full = url
        if with_token:
            full = url + ("&" if "?" in url else "?") + urlencode({"access_token": tok})
        self.requests += 1
        note_request(self.settings, self.clock())
        from hydra.integrations import http

        if self.fetch is not None:
            from hydra.watch.rt.sources import SourceError

            try:
                r = self.fetch(CID, "GET", full, {"Accept": "*/*"})
            except SourceError as e:
                raise self._map(e.status, e.message_pl, tok, e.http_status) from None
            body = r.body or b""
            if len(body) > max_bytes:
                raise MetaError("too_large", f"Plik przekracza limit {max_bytes} bajtów.")
            return int(r.status), {k.lower(): v for k, v in (r.headers or {}).items()}, body
        try:
            res = http.request(CID, "GET", full, settings=self.settings, writer=self.writer, secrets=[tok],
                               require_enabled=self.require_enabled, timeout=30.0, max_bytes=max_bytes,
                               headers={"Accept": "*/*"})
        except http.IntegrationError as e:
            if "limit" in (e.message_pl or "") and e.status == "error":
                raise MetaError("too_large", str(scrub(e.message_pl, tok))[:200]) from None
            raise self._map(e.status, e.message_pl, tok, e.http_status) from None
        return res.status_code, {k.lower(): v for k, v in res.headers.items()}, res.body

    def _send(self, full: str, tok: str) -> tuple[int, dict[str, str], bytes]:
        from hydra.integrations import http

        if self.fetch is not None:
            from hydra.watch.rt.sources import SourceError

            try:
                r = self.fetch(CID, "GET", full, {"Accept": "application/json"})
            except SourceError as e:
                raise self._map(e.status, e.message_pl, tok, e.http_status) from None
            return int(r.status), {k.lower(): v for k, v in (r.headers or {}).items()}, r.body or b""
        try:
            r = http.request(CID, "GET", full, settings=self.settings, writer=self.writer, secrets=[tok],
                             require_enabled=self.require_enabled, timeout=40.0, max_bytes=MAX_BYTES)
        except http.IntegrationError as e:
            raise self._map(e.status, e.message_pl, tok, e.http_status) from None
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.body

    def _map(self, status: str, message: str, tok: str, http_status: int | None) -> MetaError:
        msg = str(scrub(message, tok))[:300]
        if status == "offline":
            return MetaError("offline", MSG_PL["offline"])
        if status == "rate_limited":  # per-minute connector limit: short deferral, not the provider back-off
            return MetaError("rate_limited", msg, retry_at=self.clock() + 60.0)
        if status in ("disabled", "blocked"):
            return MetaError(status, MSG_PL[status] + " " + msg)
        return MetaError("error", MSG_PL["error"].format(msg=msg), http_status=http_status)


def clear_token_rejection(settings: Any) -> None:
    """A new token was saved: the persisted token_invalid / permission state no longer applies."""
    with contextlib.suppress(OSError):
        update_state(settings, token_rejected=False, token_rejected_status=None, token_fp=None)


def _fp(tok: str) -> str:
    import hashlib

    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def _retry_after(headers: dict[str, str]) -> float | None:
    v = (headers or {}).get("retry-after")
    if not v:
        return None
    try:
        return max(1.0, float(v))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            return max(1.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None
