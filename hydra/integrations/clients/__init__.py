"""Live integration clients: one small module per provider.

Each inbound module exposes `KINDS` (indicator kinds it accepts), `build(indicator, kind, keys) -> Req`,
`parse(indicator, kind, status, data) -> (verdict, details)` and `test_request(keys) -> Req`. Lookups go through
the Enricher cache/replay (`hydra.enrich.client`), source `integ-<connector>`, query `<kind>:<indicator>`, so
results replay offline; secrets are never part of a cache key, a cached URL or cached data.
Every request goes through `hydra.integrations.http.request` (offline fail-closed, netguard, rate limit, audit).
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from hydra.enrich.client import DELAY, FACT_STATUSES, Enricher, now_iso
from hydra.integrations import http, registry, secrets
from hydra.redact import redact_obj

VERDICTS = ("malicious", "suspicious", "clean", "unknown")
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
INBOUND_MODULES = {
    "urlscan": "urlscan", "virustotal": "virustotal", "urlhaus": "urlhaus", "abuseipdb": "abuseipdb",
    "google-safe-browsing": "safebrowsing",
}


@dataclass
class Req:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


def kind_of(value: str) -> str | None:
    """domain | url | ip | sha256 | None (rejected)."""
    v = (value or "").strip()
    if not v or len(v) > 2048 or any(c.isspace() for c in v):
        return None
    if v.lower().startswith(("http://", "https://")):
        try:
            return "url" if urlsplit(v).hostname else None
        except ValueError:
            return None
    try:
        ipaddress.ip_address(v)
        return "ip"
    except ValueError:
        pass
    if _SHA_RE.match(v.lower()):
        return "sha256"
    return "domain" if _HOST_RE.match(v.lower().rstrip(".")) else None


def normalise(value: str, kind: str) -> str:
    v = value.strip()
    return v.lower().rstrip(".") if kind in ("domain", "sha256") else v


def verdict(indicator: str, provider: str, v: str, details: dict[str, Any], fetched_at: str | None,
            kind: str | None = None, cached: bool | None = None) -> dict[str, Any]:
    if v not in VERDICTS:
        raise ValueError(v)
    out: dict[str, Any] = {"indicator": indicator, "kind": kind, "provider": provider, "verdict": v,
                           "details": details, "fetched_at": fetched_at}
    if cached is not None:
        out["cached"] = cached
    return out


def keys_for(cid: str) -> dict[str, str | None]:
    c = registry.get(cid)
    return {n: secrets.get_secret(cid, n) for n in (c.secrets if c else ())}


def module(cid: str) -> Any:
    name = INBOUND_MODULES.get(cid)
    return importlib.import_module(f"hydra.integrations.clients.{name}") if name else None


def scrub_obj(obj: Any, values: list[str]) -> Any:
    if not values:
        return redact_obj(obj)
    return redact_obj(json.loads(http.scrub(json.dumps(obj, ensure_ascii=False), values)))


def source(cid: str) -> str:
    src = f"integ-{cid}"
    DELAY.setdefault(src, 0.0)  # politeness is the connector rate limit in hydra.integrations.http
    return src


def lookup(cid: str, indicator: str, *, settings: Any, writer: Any, enricher: Enricher | None = None
           ) -> dict[str, Any] | None:
    """Verdict for one indicator, or None when the connector does not accept that kind."""
    mod = module(cid)
    kind = kind_of(indicator)
    if mod is None or kind is None or kind not in mod.KINDS:
        return None
    ind = normalise(indicator, kind)
    keys = keys_for(cid)
    vals = [v for v in keys.values() if v]
    enr = enricher or Enricher(settings=settings, live=True)

    def fetch() -> tuple[str | None, str, Any]:
        req = mod.build(ind, kind, keys)
        try:
            res = http.request(cid, req.method, req.url, headers=req.headers, body=req.body, settings=settings,
                               writer=writer, secrets=vals)
        except http.IntegrationError as exc:
            return http.redacted_url(req.url), f"error:{exc.status}", None
        if res.status_code == 404:
            return res.url_redacted, "404", {}
        if not res.ok:
            return res.url_redacted, f"error:http{res.status_code}", None
        data = res.json()
        if data is None:
            data = {"_text": http.scrub(res.body[:200_000].decode("utf-8", "replace"), vals)}
        return res.url_redacted, "200", scrub_obj(data, vals)

    src = source(cid)
    was_cached = enr.path(src, f"{kind}:{ind}").is_file()
    r = enr.get(src, f"{kind}:{ind}", fetch)
    if r.status not in FACT_STATUSES:
        return verdict(ind, cid, "unknown", {"status": r.status}, r.fetched_at, kind, was_cached)
    v, details = mod.parse(ind, kind, r.status, r.data)
    return verdict(ind, cid, v, details, r.fetched_at or now_iso(), kind, was_cached)
