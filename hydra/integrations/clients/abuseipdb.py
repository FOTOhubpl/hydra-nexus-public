"""AbuseIPDB API v2: abuse reports for an IP address.

GET https://api.abuseipdb.com/api/v2/check?ipAddress=<ip>&maxAgeInDays=90, header `Key`. Verdict from
`abuseConfidenceScore`: >= 75 malicious, >= 25 suspicious, otherwise unknown (few reports do not prove "clean").
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from hydra.integrations.clients import Req

KINDS = ("ip",)
BASE = "https://api.abuseipdb.com/api/v2/check"


def build(indicator: str, kind: str, keys: dict[str, str | None]) -> Req:
    return Req("GET", f"{BASE}?{urlencode({'ipAddress': indicator, 'maxAgeInDays': 90})}",
               {"Key": keys.get("ABUSEIPDB_KEY") or ""})


def test_request(keys: dict[str, str | None]) -> Req:
    return build("1.1.1.1", "ip", keys)


def parse(indicator: str, kind: str, status: str, data: Any) -> tuple[str, dict[str, Any]]:
    d = (data.get("data") or {}) if isinstance(data, dict) else {}
    score = int(d.get("abuseConfidenceScore") or 0)
    details = {"abuse_confidence": score, "total_reports": int(d.get("totalReports") or 0),
               "country": d.get("countryCode"), "isp": d.get("isp"), "usage_type": d.get("usageType"),
               "last_reported_at": d.get("lastReportedAt"), "is_whitelisted": d.get("isWhitelisted")}
    if score >= 75:
        return "malicious", details
    return ("suspicious" if score >= 25 else "unknown"), details
