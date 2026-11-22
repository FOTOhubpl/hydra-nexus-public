"""Certificate Transparency search via crt.sh (Sectigo): issuers, SAN siblings, first certificate date.

Terms: free public service, no SLA (frequent 502/timeouts); CT data is public by design (RFC 6962).
"""

from __future__ import annotations

import re
from typing import Any

from hydra.enrich.client import Enricher, Response, http_json


def lookup(enr: Enricher, d: str) -> tuple[Response, dict[str, Any]]:
    r = enr.get("crtsh", d, lambda: http_json(f"https://crt.sh/?q={d}&output=json", timeout=60, tries=3))
    return r, summarize(r.data) if r.ok else {}


def summarize(j: Any) -> dict[str, Any]:
    if not isinstance(j, list):
        return {}
    sans: set[str] = set()
    issuers: set[str] = set()
    nb: list[str] = []
    for c in j:
        for n in str(c.get("name_value", "")).split("\n"):
            n = n.strip().lower().removeprefix("*.")
            if n:
                sans.add(n)
        m = re.search(r"O=([^,]+)", str(c.get("issuer_name", "")))
        if m:
            issuers.add(m.group(1).strip().strip('"'))
        if c.get("not_before"):
            nb.append(str(c["not_before"]))
    nb.sort()
    return {
        "n_certs": len(j),
        "sans": sorted(sans)[:300],
        "issuers": sorted(issuers),
        "first_cert": nb[0] if nb else None,
        "last_cert": nb[-1] if nb else None,
    }
