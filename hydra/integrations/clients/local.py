"""Connectors served by the existing enrichment modules (read-only reuse):

- `cert-pl`: `hydra.enrich.cert_pl.lookup` on the local snapshot (no network, works offline);
- `crtsh`, `rdap`, `dns`: `hydra.enrich.{crtsh,rdap,dns}` through the replayable Enricher cache. Live lookups follow
  the existing switches (HYDRA_ENRICH=1 and HYDRA_OFFLINE=0); otherwise only cached answers are returned.

These modules keep their own vetted HTTP path (`hydra.enrich.client.http_json`, netguard per hop); the API layer
audits each run as INTEGRATION_RUN.
"""

from __future__ import annotations

import shutil
from typing import Any

from hydra.enrich.client import Enricher, live_enabled
from hydra.integrations.clients import kind_of, normalise, verdict

KINDS = {"cert-pl": ("domain", "url"), "crtsh": ("domain",), "rdap": ("domain", "ip"), "dns": ("domain",)}


def _host(indicator: str, kind: str) -> str:
    if kind == "url":
        from urllib.parse import urlsplit

        return (urlsplit(indicator).hostname or "").lower()
    return indicator


def test(cid: str, settings: Any) -> tuple[bool, str, str]:
    """(ok, status, message_pl). No network: checks the snapshot / switches / binaries."""
    if cid == "cert-pl":
        from hydra.enrich import cert_pl

        if not cert_pl.available():
            return False, "not_configured", ("Brak lokalnej migawki listy ostrzeżeń CERT Polska "
                                             "($HYDRA_DATA_DIR/external/cert/domains.json).")
        m = cert_pl.snapshot_meta()
        return True, "ok", f"Migawka listy ostrzeżeń dostępna (pobrana {m.get('retrieved_at') or 'nieznana data'})."
    if not live_enabled(settings):
        return False, "offline", ("Zapytania na żywo wymagają HYDRA_ENRICH=1 i HYDRA_OFFLINE=0; w tym trybie "
                                  "dostępne są tylko odpowiedzi z pamięci podręcznej.")
    if cid == "dns" and not shutil.which("dig"):
        return True, "ok", "Brak programu dig: rekordy A/AAAA przez resolver systemowy."
    return True, "ok", "Moduł wzbogacania gotowy (zapytania na żywo włączone)."


def lookup(cid: str, indicator: str, *, settings: Any, writer: Any = None, enricher: Enricher | None = None
           ) -> dict[str, Any] | None:
    kind = kind_of(indicator)
    if kind is None or kind not in KINDS.get(cid, ()):
        return None
    ind = normalise(indicator, kind)
    host = _host(ind, kind)
    if cid == "cert-pl":
        from hydra.enrich import cert_pl

        if not cert_pl.available():
            return verdict(ind, cid, "unknown", {"checked": False}, None, kind)
        r = cert_pl.lookup(host)
        v = "malicious" if r.get("host_listed") else ("suspicious" if r.get("parent_listed") else "unknown")
        return verdict(ind, cid, v, {"checked": True, **r}, r.get("retrieved_at"), kind, True)
    enr = enricher or Enricher(settings=settings)
    if cid == "crtsh":
        from hydra.enrich import crtsh

        resp, summary = crtsh.lookup(enr, host)
    elif cid == "rdap":
        from hydra.enrich import rdap

        resp, summary = (rdap.ip(enr, host) if kind == "ip" else rdap.domain(enr, host))
    else:
        from hydra.enrich import dns

        resp = dns.query(enr, host, "A")
        summary = {"a": dns.answers(resp)}
    return verdict(ind, cid, "unknown", {"status": resp.status, **(summary or {})}, resp.fetched_at, kind,
                   resp.cache_path is not None)
