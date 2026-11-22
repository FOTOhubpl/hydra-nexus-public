"""Outbound webhooks: endpoint store, HMAC signer, delivery worker (retry, backoff, dead letter), event dispatcher.

Storage (never the state DB, never the repo):
- endpoint config: `<secret_dir>/webhooks.json` (0600; a Slack/Teams URL is itself a credential),
- per-endpoint signing secret: `<secret_dir>/webhook-<endpoint_id>.env` (0600, via hydra.integrations.secrets),
- delivery log: `<state_dir>/integrations/deliveries.jsonl` (one snapshot per state change, last line wins).

Signature: `X-Hydra-Signature: sha256=<hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")>` with
`X-Hydra-Timestamp` (unix seconds), `X-Hydra-Event`, `X-Hydra-Delivery`. Receivers reject |now - ts| > 300 s.

Delivery policy (no silent fallback): a loopback URL (127.0.0.0/8, ::1, localhost) is allowed even offline because
the data does not leave the machine. Any other URL needs HYDRA_OFFLINE=0, the endpoint enabled and
`netguard.check_url(resolve=True)`; otherwise the delivery is `blocked_offline` with a Polish reason and no socket
is opened. Retry on network errors, 429 and 5xx (max 5 attempts, backoff base HYDRA_WEBHOOK_BACKOFF_S, default 2 s:
+2, +4, +8, +16 s); any other non-2xx answer is dead at once. Redirects are never followed.

Every attempt is audited as INTEGRATION_DELIVERY through the single Writer: host only (no path, no query), never the
secret. Event payloads carry ids, counts and indicators only: no artefact contents, no file names, no notes.
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import hmac
import ipaddress
import json
import logging
import os
import secrets as pysecrets
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hydra import __version__, events, netguard
from hydra.integrations import secrets as sec

log = logging.getLogger(__name__)

SECRET_NAME = "WEBHOOK_SIGNING_SECRET"
MAX_ATTEMPTS = 5
SIGNATURE_TOLERANCE_S = 300
RESPONSE_CAP = 64 * 1024
SINK_RECEIPTS = 50
SINK_BODY_CAP = 1024 * 1024
MAX_ENDPOINTS = 50
MIN_SECRET_LEN = 16
MAX_SECRET_LEN = 512
LOG_COMPACT_LINES = 5000
LOG_KEEP = 1000
AUDIT_TYPE = "INTEGRATION_DELIVERY"
STATUSES = ("pending", "delivered", "retrying", "dead", "blocked_offline")
USER_AGENT = f"HYDRA-NEXUS-Webhooks/{__version__}"

SCHEME_PL = ("Podpis HMAC-SHA256 liczony kluczem punktu końcowego z ciągu \"{X-Hydra-Timestamp}.{surowe ciało}\"; "
             "nagłówek X-Hydra-Signature ma postać \"sha256=<hex>\". Odbiorca porównuje podpis w czasie stałym "
             "i odrzuca żądania, których znacznik czasu różni się od bieżącego o więcej niż 300 s.")

EVENT_TYPES: tuple[dict[str, Any], ...] = (
    {"id": "ping", "label_pl": "Test połączenia (ping)",
     "description_pl": "Wysyłane wyłącznie przyciskiem „Testuj”. Sprawdza adres, podpis i odpowiedź odbiorcy.",
     "example": {"endpoint_id": "wh-3f9a1c2b7d", "note": "ping"}},
    {"id": "delta", "label_pl": "Zmiana wyników (delta)",
     "description_pl": "Zmaterializowana zmiana: zakończony przebieg analizy (nowe kampanie) albo zmiana "
                       "przypisania incydentu. Zawiera tylko identyfikatory i liczniki.",
     "example": {"source": "RUN_FINISHED", "run_id": "run-20261122-0215", "kind": "analyse", "pairs": 1284,
                 "campaigns": 37}},
    {"id": "job", "label_pl": "Zadanie zakończone",
     "description_pl": "Zakończenie zadania w tle (import, analiza): status i liczniki elementów, bez nazw plików.",
     "example": {"job_id": "job-7c1e", "kind": "import", "status": "done", "total": 120, "done": 118, "failed": 2}},
    {"id": "watch", "label_pl": "Alert HYDRA Watch",
     "description_pl": "Dopasowanie odcisku kampanii w strumieniu Watch. To kandydat do weryfikacji, "
                       "nie potwierdzony członek kampanii.",
     "example": {"alert_seq": 12, "event_id": "ct-000913", "host_defanged": "przyklad[.]pl", "event_bits": 14.2,
                 "assignment": "candidate", "alerts": 1, "candidate": True}},
    {"id": "review", "label_pl": "Decyzja analityka",
     "description_pl": "Recenzja analityka (połączenie, rozłączenie, cofnięcie). Bez notatek i bez nazwy analityka.",
     "example": {"review_id": "rv-51d2", "action": "cannot_link", "target_ids": ["inc-0042", "inc-0107"]}},
    {"id": "export", "label_pl": "Eksport danych",
     "description_pl": "Wygenerowano eksport (STIX, CSV, Neo4j, pakiet zgłoszenia): rodzaj i liczniki.",
     "example": {"kind": "stix_run", "run_id": "run-20261122-0215", "counts": {"campaigns": 37, "indicators": 412}}},
)
EVENT_IDS = tuple(e["id"] for e in EVENT_TYPES)


class WebhookError(ValueError):
    """Invalid input (message is Polish, safe to show, never contains a secret or a URL path)."""


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ helpers


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")


def backoff_base() -> float:
    try:
        return max(0.0, float(os.environ.get("HYDRA_WEBHOOK_BACKOFF_S", "2")))
    except ValueError:
        return 2.0


def http_timeout() -> float:
    try:
        return min(60.0, max(0.5, float(os.environ.get("HYDRA_WEBHOOK_TIMEOUT_S", "10"))))
    except ValueError:
        return 10.0


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """`sha256=` + hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")."""
    msg = f"{timestamp}.".encode() + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify(secret: str, timestamp: str | int | None, body: bytes, signature: str | None, *,
           tolerance_s: int = SIGNATURE_TOLERANCE_S, now: float | None = None) -> bool:
    """Constant-time signature check plus the replay window (|now - timestamp| <= tolerance_s)."""
    if not secret or not signature or timestamp is None:
        return False
    try:
        ts = int(str(timestamp).strip())
    except ValueError:
        return False
    if abs((now if now is not None else _now()) - ts) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, ts, body).encode(), str(signature).strip().encode())


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    h = host.strip().strip("[]").rstrip(".").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback") or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def url_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_url(url: str) -> bool:
    return is_loopback_host(url_host(url))


def redact_url(url: str) -> str:
    """scheme://host[:port]/… + last 4 characters of the path (never the query string)."""
    try:
        sp = urlsplit(url)
        host = sp.hostname or ""
        port = sp.port
    except ValueError:
        return "nieprawidłowy adres"
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    path = sp.path.rstrip("/")
    tail = f"/…{path[-4:]}" if len(path) > 4 else ("/…" if path else "/")
    return f"{sp.scheme}://{netloc}{tail}"


def validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2048 or any(c in url for c in "\r\n\t\x00 "):
        raise WebhookError("Nieprawidłowy adres URL (pusty, za długi albo z niedozwolonymi znakami).")
    try:
        sp = urlsplit(url)
        _ = sp.port
    except ValueError:
        raise WebhookError("Nieprawidłowy adres URL (błędny port lub składnia).") from None
    if sp.scheme.lower() not in ("http", "https"):
        raise WebhookError("Dozwolone są tylko adresy http:// i https://.")
    if not sp.hostname:
        raise WebhookError("Adres URL musi zawierać nazwę hosta.")
    if sp.username or sp.password:
        raise WebhookError("Adres URL nie może zawierać danych logowania (user:hasło@).")
    if sp.fragment:
        raise WebhookError("Adres URL nie może zawierać fragmentu (#).")
    return url


def validate_events(evs: list[str] | tuple[str, ...]) -> list[str]:
    out = []
    for e in evs or []:
        if e not in EVENT_IDS:
            raise WebhookError(f"Nieznany typ zdarzenia: {str(e)[:40]!r}. Dozwolone: {', '.join(EVENT_IDS)}.")
        if e not in out:
            out.append(e)
    return out


def validate_secret(value: str) -> str:
    v = (value or "").strip()
    if len(v) < MIN_SECRET_LEN or len(v) > MAX_SECRET_LEN or any(c in v for c in "\r\n\x00"):
        raise WebhookError(f"Klucz podpisu musi mieć od {MIN_SECRET_LEN} do {MAX_SECRET_LEN} znaków"
                           " (bez znaków nowej linii). Użyj przycisku „Generuj”.")
    return v


def validate_name(name: str) -> str:
    n = (name or "").strip()
    if not n or len(n) > 80 or any(ord(c) < 32 for c in n):
        raise WebhookError("Nazwa musi mieć od 1 do 80 znaków.")
    return n


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix, dir=path.parent)  # 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------------------------------------ endpoint store


def config_path() -> Path:
    return sec.secret_dir() / "webhooks.json"


def connector_id(endpoint_id: str) -> str:
    return f"webhook-{endpoint_id}"


class EndpointStore:
    """Endpoint config in `<secret_dir>/webhooks.json` (0600). The directory is resolved at call time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        eps = data.get("endpoints") if isinstance(data, dict) else None
        return [e for e in eps or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]

    def _save(self, eps: list[dict[str, Any]]) -> None:
        sec.secret_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(sec.secret_dir(), 0o700)
        _atomic_write(config_path(), json.dumps({"version": 1, "endpoints": eps}, ensure_ascii=False, indent=1))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._load()

    def get(self, endpoint_id: str) -> dict[str, Any] | None:
        return next((e for e in self.list() if e["id"] == endpoint_id), None)

    def create(self, name: str, url: str, evs: list[str], enabled: bool, secret: str) -> dict[str, Any]:
        name, url, evs, secret = validate_name(name), validate_url(url), validate_events(evs), validate_secret(secret)
        with self._lock:
            eps = self._load()
            if len(eps) >= MAX_ENDPOINTS:
                raise WebhookError(f"Osiągnięto limit {MAX_ENDPOINTS} punktów końcowych.")
            ep = {"id": "wh-" + pysecrets.token_hex(5), "name": name, "url": url, "events": evs,
                  "enabled": bool(enabled), "created_at": events.utcnow()}
            sec.set_secret(connector_id(ep["id"]), SECRET_NAME, secret)
            eps.append(ep)
            self._save(eps)
            return ep

    def update(self, endpoint_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            eps = self._load()
            ep = next((e for e in eps if e["id"] == endpoint_id), None)
            if ep is None:
                raise NotFound(endpoint_id)
            new = dict(ep)
            if changes.get("name") is not None:
                new["name"] = validate_name(changes["name"])
            if changes.get("url") is not None:
                new["url"] = validate_url(changes["url"])
            if changes.get("events") is not None:
                new["events"] = validate_events(changes["events"])
            if changes.get("enabled") is not None:
                new["enabled"] = bool(changes["enabled"])
            if changes.get("secret") is not None:
                sec.set_secret(connector_id(endpoint_id), SECRET_NAME, validate_secret(changes["secret"]))
            eps[eps.index(ep)] = new
            self._save(eps)
            return new

    def delete(self, endpoint_id: str) -> None:
        with self._lock:
            eps = self._load()
            if not any(e["id"] == endpoint_id for e in eps):
                raise NotFound(endpoint_id)
            self._save([e for e in eps if e["id"] != endpoint_id])
            with contextlib.suppress(OSError, sec.SecretError):
                sec.secret_file(connector_id(endpoint_id)).unlink(missing_ok=True)

    @staticmethod
    def secret(endpoint_id: str) -> str | None:
        """Internal only (signing, sink verification). Read from the endpoint's own file: no env override, so one
        environment variable can never silently become every endpoint's key."""
        try:
            path = sec.secret_file(connector_id(endpoint_id))
        except sec.SecretError:
            return None
        return sec._read_file(path).get(SECRET_NAME) or None  # noqa: SLF001 — same package

    @classmethod
    def secret_status(cls, endpoint_id: str) -> dict[str, Any]:
        v = cls.secret(endpoint_id)
        return {"configured": bool(v), "last4": sec.last4(v)}


# ------------------------------------------------------------------------------------------------ event mapping

_ID_KEYS = ("run_id", "job_id", "incident_id", "review_id", "campaign_id", "revision_of", "event_id", "kind",
            "action", "status", "decision", "error_code", "assignment")


def _safe_str(v: Any) -> str | None:
    if not isinstance(v, str) or "/" in v or "\\" in v or len(v) > 120:
        return None
    return v


def _ids_counts(payload: dict[str, Any]) -> dict[str, Any]:
    """Ids (short strings without path separators) and numeric counts only."""
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int | float):
            out[k] = v
        elif k in _ID_KEYS and (s := _safe_str(v)) is not None:
            out[k] = s
    return out


def _target_ids(target: Any) -> list[str]:
    vals: list[Any] = []
    if isinstance(target, dict):
        for v in target.values():
            vals.extend(v if isinstance(v, list) else [v])
    elif isinstance(target, list):
        vals = target
    return [s for v in vals[:50] if (s := _safe_str(v)) is not None]


def map_event(type_: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Audit event -> (webhook event type, data) or None. Data holds ids/counts/indicators only."""
    if type_ in ("RUN_FINISHED", "REVISION_PUBLISHED") or type_.startswith("ASSIGNMENT"):
        data = _ids_counts(payload)
        if isinstance(payload.get("campaigns"), list):
            data["campaign_ids"] = [s for v in payload["campaigns"][:50] if (s := _safe_str(v))]
        return "delta", {"source": type_, **data}
    if type_ == "REVIEW":
        return "review", {"review_id": _safe_str(payload.get("id")), "action": _safe_str(payload.get("action")),
                          "target_ids": _target_ids(payload.get("target")),
                          **({"undoes": s} if (s := _safe_str(payload.get("undoes"))) else {})}
    if type_ == "EXPORT":
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        return "export", {"kind": _safe_str(payload.get("kind")), "run_id": _safe_str(payload.get("run_id")),
                          "counts": {str(k): v for k, v in counts.items()
                                     if isinstance(v, int) and not isinstance(v, bool)}}
    if type_ in ("JOB_DONE", "JOB_FAILED"):
        return "job", {"status": "done" if type_ == "JOB_DONE" else "failed", **_ids_counts(payload)}
    return None


def map_watch_alert(a: dict[str, Any]) -> dict[str, Any]:
    alerts = a.get("alerts")
    return {"alert_seq": a.get("alert_seq"), "event_id": _safe_str(a.get("event_id")),
            "host_defanged": _safe_str(a.get("host_defanged")), "first_seen": _safe_str(a.get("first_seen")),
            "event_bits": a.get("event_bits") if isinstance(a.get("event_bits"), int | float) else None,
            "assignment": _safe_str(a.get("assignment")), "replay": bool(a.get("replay")),
            "alerts": len(alerts) if isinstance(alerts, list) else 0, "candidate": True}


# ------------------------------------------------------------------------------------------------ manager


def _no_redirects() -> urllib.request.OpenerDirector:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a: Any, **k: Any) -> None:  # noqa: ANN401
            return None  # a 3xx surfaces as HTTPError: never followed (SSRF via redirect)

    # ProxyHandler({}) : never route through HTTP(S)_PROXY from the environment.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _no_redirects().open(req, timeout=timeout) as resp:
            return resp.status, resp.read(RESPONSE_CAP)
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(Exception):
            data = exc.read(RESPONSE_CAP)
            return exc.code, data
        return exc.code, b""


class WebhookManager:
    """Owns the delivery log, the delivery worker and the event dispatcher. One per app (app.state.webhooks)."""

    def __init__(self, settings: Any, writer: Any = None, *, watch_getter: Callable[[], Any] | None = None,
                 transport: Transport | None = None, poll_s: float | None = None) -> None:
        self.settings = settings
        self.writer = writer
        self.store = EndpointStore()
        self.transport: Transport = transport or urllib_transport
        self.watch_getter = watch_getter
        self.poll_s = poll_s if poll_s is not None else float(os.environ.get("HYDRA_WEBHOOK_POLL_S", "1.0"))
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._deliveries: dict[str, dict[str, Any]] = {}
        self._in_flight: set[str] = set()
        self._heap: list[tuple[float, str]] = []
        self._log_lines = 0
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._dispatcher: threading.Thread | None = None
        self._dispatch_stop = threading.Event()
        self.receipts: deque[dict[str, Any]] = deque(maxlen=SINK_RECEIPTS)
        self._load_log()
        for d in self._deliveries.values():
            if d["status"] in ("pending", "retrying"):
                self._schedule(d["id"], d.get("_due") or _now())

    # ---------------------------------------------------------------- policy
    def offline(self) -> bool:
        return netguard.offline(self.settings)

    # ---------------------------------------------------------------- delivery log
    @property
    def log_path(self) -> Path:
        return Path(self.settings.state_dir) / "integrations" / "deliveries.jsonl"

    def _load_log(self) -> None:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        self._log_lines = len(lines)
        for line in lines:
            with contextlib.suppress(ValueError, TypeError, KeyError):
                d = json.loads(line)
                self._deliveries[d["id"]] = d

    def _persist(self, d: dict[str, Any]) -> None:
        """Append one snapshot (caller holds the lock). Compacts the file past LOG_COMPACT_LINES."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self._log_lines >= LOG_COMPACT_LINES:
            keep = sorted(self._deliveries.values(), key=lambda x: x["created_at"])[-LOG_KEEP:]
            self._deliveries = {x["id"]: x for x in keep}
            _atomic_write(self.log_path, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in keep))
            self._log_lines = len(keep)
            return
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
        self._log_lines += 1

    @staticmethod
    def public(d: dict[str, Any]) -> dict[str, Any]:
        return {k: d.get(k) for k in ("id", "endpoint_id", "event", "status", "attempts", "max_attempts",
                                      "last_status_code", "last_error", "next_retry_at", "payload_sha256",
                                      "created_at", "delivered_at")}

    def deliveries(self, endpoint_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = [d for d in self._deliveries.values() if endpoint_id is None or d["endpoint_id"] == endpoint_id]
        rows.sort(key=lambda d: d["created_at"], reverse=True)
        return [self.public(d) for d in rows[:limit]]

    def get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self._lock:
            d = self._deliveries.get(delivery_id)
            return self.public(d) if d else None

    def last_delivery(self, endpoint_id: str) -> dict[str, Any] | None:
        got = self.deliveries(endpoint_id, 1)
        return got[0] if got else None

    # ---------------------------------------------------------------- enqueue
    def create_delivery(self, endpoint_id: str, event: str, data: dict[str, Any]) -> dict[str, Any]:
        did = "dlv-" + pysecrets.token_hex(8)
        created = events.utcnow()
        body = json.dumps({"id": did, "event": event, "created_at": created, "hydra_version": __version__,
                           "data": data}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        d = {"id": did, "endpoint_id": endpoint_id, "event": event, "status": "pending", "attempts": 0,
             "max_attempts": MAX_ATTEMPTS, "last_status_code": None, "last_error": None, "next_retry_at": None,
             "payload_sha256": hashlib.sha256(body.encode()).hexdigest(), "created_at": created,
             "delivered_at": None, "body": body}
        with self._lock:
            self._deliveries[did] = d
            self._persist(d)
        return d

    def _schedule(self, delivery_id: str, due: float) -> None:
        with self._cv:
            heapq.heappush(self._heap, (due, delivery_id))
            self._cv.notify_all()
        self._ensure_worker()

    def enqueue(self, endpoint_id: str, event: str, data: dict[str, Any]) -> dict[str, Any]:
        d = self.create_delivery(endpoint_id, event, data)
        self._schedule(d["id"], _now())
        return self.public(d)

    def send_test(self, endpoint_id: str) -> dict[str, Any]:
        """`ping`: first attempt runs synchronously (the UI shows the result), retries go to the worker."""
        if self.store.get(endpoint_id) is None:
            raise NotFound(endpoint_id)
        d = self.create_delivery(endpoint_id, "ping", {"endpoint_id": endpoint_id, "note": "ping"})
        return self.attempt(d["id"])

    def retry(self, delivery_id: str) -> dict[str, Any]:
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is None:
                raise NotFound(delivery_id)
            if d["status"] == "delivered":
                raise Conflict("Dostarczenie zakończyło się sukcesem; ponowienie nie jest potrzebne.")
            if delivery_id in self._in_flight:
                raise Conflict("Dostarczenie jest właśnie wysyłane.")
            d.update(status="pending", attempts=0, next_retry_at=None, last_error=None)
            self._persist(d)
        return self.attempt(delivery_id)

    # ---------------------------------------------------------------- one attempt
    def _block_reason(self, ep: dict[str, Any] | None) -> str | None:
        if ep is None:
            return "Punkt końcowy został usunięty."
        if is_loopback_url(ep["url"]):
            return None  # data stays on this machine: allowed even offline
        if self.offline():
            return ("Tryb offline (HYDRA_OFFLINE=1): wysyłka poza ten komputer jest zablokowana. Ustaw "
                    "HYDRA_OFFLINE=0 i włącz punkt końcowy, aby wysyłać na adresy zewnętrzne.")
        if not ep.get("enabled"):
            return "Punkt końcowy jest wyłączony: wysyłka na adres zewnętrzny wymaga włączenia."
        try:
            netguard.check_url(ep["url"], resolve=True)
        except netguard.UnsafeDestination:
            return ("Adres docelowy wskazuje na sieć prywatną, lokalną lub nie daje się rozwiązać (ochrona SSRF): "
                    "wysyłka zablokowana.")
        except OSError:
            return "Nie udało się sprawdzić adresu docelowego (DNS): wysyłka zablokowana."
        return None

    def attempt(self, delivery_id: str) -> dict[str, Any]:
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is None:
                raise NotFound(delivery_id)
            if delivery_id in self._in_flight or d["status"] not in ("pending", "retrying"):
                return self.public(d)
            self._in_flight.add(delivery_id)
        try:
            return self._attempt(d)
        finally:
            with self._lock:
                self._in_flight.discard(delivery_id)

    def _attempt(self, d: dict[str, Any]) -> dict[str, Any]:
        ep = self.store.get(d["endpoint_id"])
        host = url_host(ep["url"]) if ep else ""
        loopback = bool(ep) and is_loopback_url(ep["url"])
        reason = self._block_reason(ep)
        secret = self.store.secret(d["endpoint_id"]) if ep else None
        if reason is None and not secret:
            reason = "Brak klucza podpisu dla punktu końcowego."
        if reason is not None:
            with self._lock:
                d.update(status="blocked_offline" if ep else "dead", last_error=reason, next_retry_at=None,
                         last_status_code=None)
                self._persist(d)
            self._audit(d, host, loopback, outcome=d["status"], code=None, ms=0, bytes_out=0, bytes_in=0)
            return self.public(d)
        assert ep is not None and secret is not None
        body = d["body"].encode()
        ts = int(_now())
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, "X-Hydra-Event": d["event"],
                   "X-Hydra-Delivery": d["id"], "X-Hydra-Timestamp": str(ts),
                   "X-Hydra-Signature": sign(secret, ts, body)}
        t0 = time.perf_counter()
        code: int | None = None
        resp = b""
        error: str | None = None
        try:
            code, resp = self.transport(ep["url"], body, headers, http_timeout())
        except TimeoutError:
            error = "Przekroczono czas oczekiwania na odpowiedź odbiorcy."
        except netguard.OfflineBlocked:
            error = "Połączenie zablokowane przez tryb offline (strażnik gniazd)."
        except urllib.error.URLError as exc:
            error = f"Błąd sieci: {type(exc.reason).__name__ if not isinstance(exc.reason, str) else 'URLError'}."
        except (OSError, ValueError) as exc:
            error = f"Błąd sieci: {type(exc).__name__}."
        ms = round((time.perf_counter() - t0) * 1000, 1)
        with self._lock:
            d["attempts"] += 1
            d["last_status_code"] = code
            if code is not None and 200 <= code < 300:
                d.update(status="delivered", last_error=None, next_retry_at=None, delivered_at=events.utcnow())
            else:
                retryable = code is None or code == 429 or code >= 500
                if code is not None:
                    error = (f"Odbiorca odpowiedział kodem {code}" + (" (przekierowania nie są obsługiwane)"
                                                                     if 300 <= code < 400 else "") + ".")
                d["last_error"] = error
                if retryable and d["attempts"] < d["max_attempts"]:
                    due = _now() + backoff_base() * (2 ** (d["attempts"] - 1))
                    d.update(status="retrying", next_retry_at=_iso(due), _due=due)
                else:
                    if retryable:
                        d["last_error"] = f"{error} Wyczerpano {d['max_attempts']} prób (dead letter)."
                    d.update(status="dead", next_retry_at=None)
            self._persist(d)
            status = d["status"]
        self._audit(d, host, loopback, outcome=status, code=code, ms=ms, bytes_out=len(body), bytes_in=len(resp))
        if status == "retrying":
            self._schedule(d["id"], d["_due"])
        return self.public(d)

    def _audit(self, d: dict[str, Any], host: str, loopback: bool, *, outcome: str, code: int | None, ms: float,
               bytes_out: int, bytes_in: int) -> None:
        if self.writer is None:
            return
        payload = {"delivery_id": d["id"], "endpoint_id": d["endpoint_id"], "event": d["event"], "host": host,
                   "loopback": loopback, "attempt": d["attempts"], "status": outcome, "status_code": code, "ms": ms,
                   "bytes_out": bytes_out, "bytes_in": bytes_in, "payload_sha256": d["payload_sha256"]}
        try:
            self.writer.submit(lambda conn: events.append(conn, AUDIT_TYPE, payload)).result(timeout=30)
        except Exception:  # noqa: BLE001 — a delivery must not fail on audit logging; logged without the URL
            log.warning("webhook delivery %s attempt not audited", d["id"], exc_info=True)

    # ---------------------------------------------------------------- worker
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._stop.is_set() or (self._worker is not None and self._worker.is_alive()):
                return
            self._worker = threading.Thread(target=self._work, name="hydra-webhook-worker", daemon=True)
            self._worker.start()

    def _work(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                if not self._heap:
                    self._cv.wait(timeout=5)
                    continue
                due, did = self._heap[0]
                wait = due - _now()
                if wait > 0:
                    self._cv.wait(timeout=min(wait, 5))
                    continue
                heapq.heappop(self._heap)
            if self._stop.is_set():
                return
            try:
                self.attempt(did)
            except Exception:  # noqa: BLE001 — never let one delivery kill the worker
                log.warning("webhook delivery %s failed unexpectedly", did, exc_info=True)

    # ---------------------------------------------------------------- dispatcher
    def any_enabled(self) -> bool:
        return any(e.get("enabled") for e in self.store.list())

    def dispatcher_running(self) -> bool:
        return self._dispatcher is not None and self._dispatcher.is_alive()

    def sync_dispatcher(self) -> None:
        """Start the dispatcher when at least one endpoint is enabled; stop it when none is."""
        if self._stop.is_set():
            return
        if self.any_enabled():
            with self._lock:
                if not self.dispatcher_running():
                    self._dispatch_stop = threading.Event()
                    self._dispatcher = threading.Thread(target=self._dispatch, args=(self._dispatch_stop,),
                                                        name="hydra-webhook-dispatcher", daemon=True)
                    self._dispatcher.start()
        else:
            self._stop_dispatcher()

    def _stop_dispatcher(self) -> None:
        t = self._dispatcher
        self._dispatch_stop.set()
        if t is not None and t is not threading.current_thread():
            t.join(timeout=10)
        self._dispatcher = None

    def _connect(self) -> sqlite3.Connection | None:
        from hydra.db import connect

        try:
            return connect(Path(self.settings.db_path), readonly=True)
        except sqlite3.Error:
            return None

    def _dispatch(self, stop: threading.Event) -> None:
        conn = self._connect()
        cursor = 0
        alert_seq = 0
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                cursor = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        eng = self.watch_getter() if self.watch_getter else None
        if eng is not None:
            with contextlib.suppress(Exception):
                rows = eng.alerts_since(0, 1000)
                alert_seq = max((r["alert_seq"] for r in rows), default=0)
        try:
            while not stop.wait(self.poll_s):
                if conn is None:
                    conn = self._connect()
                    continue
                cursor, alert_seq = self.poll_once(conn, cursor, alert_seq)
        finally:
            if conn is not None:
                conn.close()

    def poll_once(self, conn: sqlite3.Connection, cursor: int, alert_seq: int = 0) -> tuple[int, int]:
        """Read audit events after `cursor` and watch alerts after `alert_seq`, fan out to subscribed endpoints."""
        out: list[tuple[str, dict[str, Any]]] = []
        try:
            rows = conn.execute("SELECT seq, type, payload FROM events WHERE seq > ? ORDER BY seq LIMIT 500",
                                (cursor,)).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            cursor = r["seq"]
            try:
                payload = json.loads(r["payload"])
            except ValueError:
                continue
            mapped = map_event(r["type"], payload if isinstance(payload, dict) else {})
            if mapped is None:
                continue
            ev, data = mapped
            if ev == "job" and data.get("job_id"):
                with contextlib.suppress(sqlite3.Error):
                    row = conn.execute("SELECT kind FROM jobs WHERE id=?", (data["job_id"],)).fetchone()
                    if row and _safe_str(row["kind"]):
                        data["kind"] = row["kind"]
            out.append((ev, {"audit_seq": r["seq"], **data}))
        eng = self.watch_getter() if self.watch_getter else None
        if eng is not None:
            with contextlib.suppress(Exception):
                for a in eng.alerts_since(alert_seq, 200):
                    alert_seq = max(alert_seq, int(a["alert_seq"]))
                    out.append(("watch", map_watch_alert(a)))
        if out:
            eps = [e for e in self.store.list() if e.get("enabled")]
            for ev, data in out:
                for ep in eps:
                    if ev in ep.get("events", []):
                        self.enqueue(ep["id"], ev, data)
        return cursor, alert_seq

    # ---------------------------------------------------------------- sink
    def sink_receive(self, endpoint_id: str | None, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        secret = self.store.secret(endpoint_id) if endpoint_id and self.store.get(endpoint_id) else None
        ok = bool(secret) and verify(secret or "", headers.get("x-hydra-timestamp"), body,
                                     headers.get("x-hydra-signature"))
        def short(v: str | None) -> str | None:
            return (_safe_str(v) or "")[:40] or None

        rec = {"at": events.utcnow(), "endpoint_id": short(endpoint_id), "event": short(headers.get("x-hydra-event")),
               "delivery_id": short(headers.get("x-hydra-delivery")), "signature_ok": ok, "bytes": len(body)}
        with self._lock:
            self.receipts.appendleft(rec)
        return rec

    def sink_receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.receipts)

    # ---------------------------------------------------------------- lifecycle
    def stop(self) -> None:
        self._stop.set()
        self._stop_dispatcher()
        with self._cv:
            self._cv.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=10)
