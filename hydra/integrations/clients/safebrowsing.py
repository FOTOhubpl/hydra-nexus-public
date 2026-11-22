"""Google Safe Browsing v4 Lookup API (threatMatches:find), non-commercial use only and deprecated (Web Risk is the
commercial API). The key travels as the `key` query parameter; `http.redacted_url` drops the query, so it never
reaches the audit log or the cache. A domain is checked as `http://<domain>/`. `matches` -> malicious; an empty
answer `{}` -> unknown (not listed is not "clean").
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from hydra.integrations.clients import Req

KINDS = ("url", "domain")
BASE = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
THREATS = ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"]


def build(indicator: str, kind: str, keys: dict[str, str | None]) -> Req:
    url = indicator if kind == "url" else f"http://{indicator}/"
    body = {"client": {"clientId": "hydra-nexus", "clientVersion": "1.0"},
            "threatInfo": {"threatTypes": THREATS, "platformTypes": ["ANY_PLATFORM"], "threatEntryTypes": ["URL"],
                           "threatEntries": [{"url": url}]}}
    return Req("POST", f"{BASE}?key={quote(keys.get('GOOGLE_SAFE_BROWSING_KEY') or '', safe='')}",
               {"Content-Type": "application/json"}, json.dumps(body).encode())


def test_request(keys: dict[str, str | None]) -> Req:
    return build("http://example.com/", "url", keys)


def parse(indicator: str, kind: str, status: str, data: Any) -> tuple[str, dict[str, Any]]:
    matches = (data.get("matches") or []) if isinstance(data, dict) else []
    details = {"matches": len(matches),
               "threat_types": sorted({m.get("threatType") for m in matches if m.get("threatType")})}
    return ("malicious" if matches else "unknown"), details
