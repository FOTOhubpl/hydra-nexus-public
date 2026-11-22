"""Meta Ad Library (Graph API `ads_archive`), wired to the existing module and scheduler.

- Test: layered importer-contract diagnostics (`hydra.meta.diagnose`) through the shared META-COLLECT transport
  (offline fail-closed, HYDRA budget, netguard, audit with the path only; the token is a query parameter and never
  reaches the audit log, the cache or a message).
- Run: one cycle of the single Meta collector (`hydra.meta.collector.run_cycle`).
"""

from __future__ import annotations

from typing import Any

from hydra.enrich.adlibrary import MESSAGE_NO_TOKEN_PL
from hydra.integrations import secrets

CID = "meta-adlibrary"
TOKEN = "META_AD_LIBRARY_TOKEN"


def token() -> str | None:
    return secrets.meta_token()


def test(settings: Any, writer: Any) -> tuple[bool, str, str]:
    """Layered importer-contract diagnostics (hydra.meta.diagnose), not just `fields=id`: OK only when the token,
    the PL/ALL query and the full minimal importer field set are accepted."""
    from hydra.meta import diagnose

    if not token():
        return False, "not_configured", MESSAGE_NO_TOKEN_PL
    d = diagnose.run(settings, writer)
    parts = [f"{x['label_pl']}: {x['status']}" for x in d["layers"]]
    bad = next((x for x in d["layers"] if x["status"] == "fail"), None)
    if d["ok"]:
        return True, "ok", "Kontrakt importu działa. " + "; ".join(parts)
    msg = (bad or {}).get("message_pl") or next((x["message_pl"] for x in d["layers"] if x["status"] == "not_run"),
                                                "")
    return False, "error", ("Import niesprawny. " + "; ".join(parts) + ". " + msg)[:900]


def run(settings: Any, writer: Any) -> dict[str, Any]:
    """One cycle of the single Meta collector (META-COLLECT): same lock, budget, cursors and analysis outbox as the
    WATCH-RT schedule and "Skanuj teraz"."""
    from hydra.meta.collector import run_cycle

    if not token():
        return {"ok": False, "status": "not_configured", "message_pl": MESSAGE_NO_TOKEN_PL, "results": []}
    res = run_cycle(settings, writer, trigger="integrations_run")
    ok = res.status in ("ok", "empty", "partial")
    return {"ok": ok, "status": "ok" if ok else res.status, "results": [res.to_dict()],
            "message_pl": f"{res.message_pl} Rekordy {res.records}, nowe rewizje {res.new_revisions}, żądania "
                          f"{res.requests}."}
