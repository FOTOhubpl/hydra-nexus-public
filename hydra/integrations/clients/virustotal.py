"""VirusTotal API v3: reputation of a domain, URL, IP address or file hash (never the file itself).

GET https://www.virustotal.com/api/v3/{domains|urls|ip_addresses|files}/<id>, header `x-apikey`. URL ids are
unpadded base64url of the URL. Verdict from `last_analysis_stats`: >= 2 engines malicious -> malicious; 1
malicious or any suspicious -> suspicious; harmless > 0 and nothing flagged -> clean; 404 -> unknown.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from hydra.integrations.clients import Req

KINDS = ("domain", "url", "ip", "sha256")
BASE = "https://www.virustotal.com/api/v3"
_PATH = {"domain": "domains", "ip": "ip_addresses", "sha256": "files", "url": "urls"}


def url_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def build(indicator: str, kind: str, keys: dict[str, str | None]) -> Req:
    ident = url_id(indicator) if kind == "url" else quote(indicator, safe="")
    return Req("GET", f"{BASE}/{_PATH[kind]}/{ident}", {"x-apikey": keys.get("VT_API_KEY") or ""})


def test_request(keys: dict[str, str | None]) -> Req:
    return build("example.com", "domain", keys)


def parse(indicator: str, kind: str, status: str, data: Any) -> tuple[str, dict[str, Any]]:
    if status == "404" or not isinstance(data, dict) or not data.get("data"):
        return "unknown", {"found": False}
    attrs = (data.get("data") or {}).get("attributes") or {}
    st = attrs.get("last_analysis_stats") or {}
    mal, sus, harm = int(st.get("malicious", 0)), int(st.get("suspicious", 0)), int(st.get("harmless", 0))
    details = {"found": True, "malicious": mal, "suspicious": sus, "harmless": harm,
               "undetected": int(st.get("undetected", 0)), "reputation": attrs.get("reputation"),
               "last_analysis_date": attrs.get("last_analysis_date"), "categories": attrs.get("categories") or {}}
    if mal >= 2:
        return "malicious", details
    if mal == 1 or sus > 0:
        return "suspicious", details
    return ("clean" if harm > 0 else "unknown"), details
