"""URLhaus (abuse.ch) API: is a host or URL listed as a malware distribution URL?

POST https://urlhaus-api.abuse.ch/v1/host/ (form `host=`) or /v1/url/ (form `url=`), header `Auth-Key`
(mandatory since 2025-06-30). `query_status: ok` with URLs -> malicious when any URL is `online`, else suspicious
(only offline history); `no_results` -> unknown (absence from one list is not "clean").
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from hydra.integrations.clients import Req

KINDS = ("domain", "ip", "url")
BASE = "https://urlhaus-api.abuse.ch/v1"


def build(indicator: str, kind: str, keys: dict[str, str | None]) -> Req:
    name = "url" if kind == "url" else "host"
    return Req("POST", f"{BASE}/{name}/", {"Auth-Key": keys.get("URLHAUS_AUTH_KEY") or "",
                                            "Content-Type": "application/x-www-form-urlencoded"},
               urlencode({name: indicator}).encode())


def test_request(keys: dict[str, str | None]) -> Req:
    return build("example.com", "domain", keys)


def parse(indicator: str, kind: str, status: str, data: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(data, dict):
        return "unknown", {"query_status": None}
    qs = data.get("query_status")
    if qs != "ok":
        return "unknown", {"query_status": qs}
    urls = data.get("urls") or ([data] if kind == "url" else [])
    online = sum(1 for u in urls if u.get("url_status") == "online")
    threats = sorted({u.get("threat") for u in urls if u.get("threat")})
    tags = sorted({t for u in urls for t in (u.get("tags") or [])})
    dates = sorted(str(u.get("date_added")) for u in urls if u.get("date_added"))
    details = {"query_status": qs, "url_count": int(data.get("url_count") or len(urls)), "online": online,
               "threats": threats, "tags": tags[:20],
               "first_seen": data.get("firstseen") or (dates[0] if dates else None),
               "blacklists": data.get("blacklists") or {}, "reference": data.get("urlhaus_reference")}
    return ("malicious" if online else "suspicious"), details
