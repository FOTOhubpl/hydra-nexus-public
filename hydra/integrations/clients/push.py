"""Optional pushes for export-format connectors: MISP, Elasticsearch, Splunk HEC, Slack.

The destination base URL is itself stored as a secret (`MISP_URL`, `ES_URL`, `SPLUNK_HEC_URL`,
`SLACK_WEBHOOK_URL`): it may point at internal infrastructure or, for Slack, *be* the credential. Every request
goes through `hydra.integrations.http.request` (offline fail-closed, netguard with resolution: private or loopback
instance addresses are refused, rate limit, audit with host + path only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hydra.integrations import exports, http
from hydra.integrations.clients import Req, keys_for


@dataclass(frozen=True)
class Target:
    url_secret: str
    key_secret: str | None
    fmt: str
    path: str
    test_path: str | None
    auth: str  # header template, "{key}" replaced


TARGETS: dict[str, Target] = {
    "misp": Target("MISP_URL", "MISP_KEY", "misp", "/events/add", "/servers/getVersion", "{key}"),
    "elasticsearch": Target("ES_URL", "ES_API_KEY", "es_bulk", "/_bulk", "/", "ApiKey {key}"),
    "splunk-hec": Target("SPLUNK_HEC_URL", "SPLUNK_HEC_TOKEN", "siem_json", "/services/collector/event",
                         "/services/collector/health", "Splunk {key}"),
    "slack": Target("SLACK_WEBHOOK_URL", None, "slack", "", None, ""),
}


class NotConfigured(Exception):
    pass


def _base(cid: str) -> tuple[Target, str, str | None, list[str]]:
    t = TARGETS[cid]
    keys = keys_for(cid)
    base = (keys.get(t.url_secret) or "").rstrip("/")
    key = keys.get(t.key_secret) if t.key_secret else None
    if not base or (t.key_secret and not key):
        missing = [n for n in (t.url_secret, t.key_secret) if n and not keys.get(n)]
        raise NotConfigured("Brak sekretów: " + ", ".join(missing) + ".")
    return t, base, key, [v for v in keys.values() if v]


def _headers(t: Target, key: str | None, content_type: str) -> dict[str, str]:
    h = {"Content-Type": content_type}
    if t.key_secret and key:
        h["Authorization"] = t.auth.format(key=key)
    return h


def request_for(cid: str, payload: exports.Payload) -> tuple[Req, list[str]]:
    t, base, key, vals = _base(cid)
    ct = "application/x-ndjson" if t.fmt == "es_bulk" else "application/json"
    return Req("POST", base + t.path, _headers(t, key, ct), payload.body), vals


def payload_for(cid: str, settings: Any, *, run_id: str | None, campaign_id: str | None,
                limit: int | None = None) -> exports.Payload:
    t = TARGETS[cid]
    if t.fmt == "slack":
        return exports.slack_message(exports.collect(settings, run_id, campaign_id, limit=limit))
    return exports.build(settings, t.fmt, run_id=run_id, campaign_id=campaign_id, limit=limit)


def test(cid: str, settings: Any, writer: Any) -> tuple[bool, str, str]:
    try:
        t, base, key, vals = _base(cid)
    except NotConfigured as exc:
        return False, "not_configured", str(exc)
    if t.test_path is None:  # Slack: a visible test message is the only test the webhook offers
        body = b'{"text": "HYDRA NEXUS: test polaczenia (bez danych)."}'
        res = http.request(cid, "POST", base, headers={"Content-Type": "application/json"}, body=body,
                           settings=settings, writer=writer, secrets=vals, require_enabled=False)
    else:
        res = http.request(cid, "GET", base + t.test_path, headers=_headers(t, key, "application/json"),
                           settings=settings, writer=writer, secrets=vals, require_enabled=False)
    if res.ok:
        return True, "ok", f"Połączenie działa (HTTP {res.status_code})."
    return False, "error", f"Serwer odpowiedział HTTP {res.status_code}."


def push(cid: str, settings: Any, writer: Any, *, run_id: str | None, campaign_id: str | None,
         limit: int | None = None) -> dict[str, Any]:
    payload = payload_for(cid, settings, run_id=run_id, campaign_id=campaign_id, limit=limit)
    try:
        req, vals = request_for(cid, payload)
    except NotConfigured as exc:
        return {"ok": False, "status": "not_configured", "message_pl": str(exc), "results": []}
    res = http.request(cid, req.method, req.url, headers=req.headers, body=req.body, settings=settings,
                       writer=writer, secrets=vals)
    ok = res.ok
    detail: dict[str, Any] = {"status_code": res.status_code, "bytes": len(payload.body), "sha256": payload.sha256,
                              "fields_sent": payload.fields_sent}
    j = res.json()
    if cid == "elasticsearch" and isinstance(j, dict):
        ok = ok and not j.get("errors")
        detail["items"] = len(j.get("items") or [])
    if cid == "misp" and isinstance(j, dict):
        detail["event_id"] = (j.get("Event") or {}).get("id")
    if cid == "splunk-hec" and isinstance(j, dict):
        ok = ok and j.get("code", 0) == 0
    msg = (f"Wysłano {len(payload.body)} B do {http.redacted_url(req.url).split('/')[2]} (HTTP {res.status_code})."
           if ok else f"Wysyłka nieudana (HTTP {res.status_code}).")
    return {"ok": ok, "status": "ok" if ok else "error", "message_pl": http.scrub(msg, vals), "results": [],
            "sent": detail}
