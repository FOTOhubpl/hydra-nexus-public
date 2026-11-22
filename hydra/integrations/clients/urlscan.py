"""urlscan.io search API: existing public scans of a domain (no new scan is submitted).

GET https://urlscan.io/api/v1/search/?q=domain:<d>&size=10, header `API-Key`. Verdict: `malicious` when any
returned scan carries `verdicts.overall.malicious`, else `unknown` (a scan without a verdict is not "clean").
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from hydra.integrations.clients import Req

KINDS = ("domain",)
BASE = "https://urlscan.io/api/v1/search/"


def build(indicator: str, kind: str, keys: dict[str, str | None]) -> Req:
    return Req("GET", f"{BASE}?q={quote('domain:' + indicator)}&size=10",
               {"API-Key": keys.get("URLSCAN_API_KEY") or ""})


def test_request(keys: dict[str, str | None]) -> Req:
    return build("example.com", "domain", keys)


def parse(indicator: str, kind: str, status: str, data: Any) -> tuple[str, dict[str, Any]]:
    data = data if isinstance(data, dict) else {}
    results = data.get("results") or []
    times = sorted(str((r.get("task") or {}).get("time")) for r in results if (r.get("task") or {}).get("time"))
    malicious = sum(1 for r in results if ((r.get("verdicts") or {}).get("overall") or {}).get("malicious"))
    ips = sorted({(r.get("page") or {}).get("ip") for r in results if (r.get("page") or {}).get("ip")})
    asns = sorted({(r.get("page") or {}).get("asn") for r in results if (r.get("page") or {}).get("asn")})
    details = {"total": data.get("total", len(results)), "returned": len(results), "malicious_scans": malicious,
               "first_seen": times[0] if times else None, "last_seen": times[-1] if times else None,
               "ips": ips[:10], "asns": asns[:10], "scan_ids": [r.get("_id") for r in results[:5] if r.get("_id")]}
    return ("malicious" if malicious else "unknown"), details
