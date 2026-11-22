"""OpenPhish community feed: download the public text feed, match indicators LOCALLY.

Nothing about our indicators leaves the machine: the only request is a plain GET of the public feed file
(`FEED_URL`, refreshed every 12 h upstream, at most once per hour here). The feed is kept under
`<state_dir>/integrations/openphish/feed.txt` (+ `meta.json`) and replays offline. Matching: exact URL ->
malicious; same host as a listed URL -> suspicious (free hosting shares hosts); otherwise unknown.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hydra.enrich.client import now_iso
from hydra.integrations import http
from hydra.integrations.clients import verdict

CID = "openphish"
FEED_URL = "https://raw.githubusercontent.com/openphish/public_feed/refs/heads/main/feed.txt"
KINDS = ("url", "domain")
MAX_AGE_S = 3600.0
MAX_FEED_BYTES = 20_000_000


def feed_dir(settings: Any) -> Path:
    return Path(settings.state_dir) / "integrations" / "openphish"


def meta(settings: Any) -> dict[str, Any]:
    try:
        return json.loads((feed_dir(settings) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def parse_feed(text: str) -> tuple[set[str], set[str]]:
    urls, hosts = set(), set()
    for line in text.splitlines():
        u = line.strip()
        if not u.lower().startswith(("http://", "https://")):
            continue
        urls.add(u)
        try:
            h = urlsplit(u).hostname
        except ValueError:
            h = None
        if h:
            hosts.add(h.lower().rstrip("."))
    return urls, hosts


def refresh(settings: Any, writer: Any, *, require_enabled: bool = True, force: bool = False) -> dict[str, Any]:
    """Download the feed unless the local copy is younger than MAX_AGE_S. Raises http.IntegrationError."""
    m = meta(settings)
    if not force and m.get("fetched_ts") and time.time() - float(m["fetched_ts"]) < MAX_AGE_S:
        return {**m, "refreshed": False}
    res = http.request(CID, "GET", FEED_URL, headers={"Accept": "text/plain"}, settings=settings, writer=writer,
                       require_enabled=require_enabled, max_bytes=MAX_FEED_BYTES)
    if not res.ok:
        raise http.IntegrationError("error", f"OpenPhish zwrócił HTTP {res.status_code}.", res.status_code)
    text = res.body.decode("utf-8", "replace")
    urls, hosts = parse_feed(text)
    d = feed_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    (d / "feed.txt").write_text(text, encoding="utf-8")
    m = {"fetched_at": now_iso(), "fetched_ts": time.time(), "entries": len(urls), "hosts": len(hosts),
         "sha256": hashlib.sha256(res.body).hexdigest(), "source": FEED_URL}
    (d / "meta.json").write_text(json.dumps(m, indent=1), encoding="utf-8")
    return {**m, "refreshed": True}


def load(settings: Any) -> tuple[set[str], set[str], dict[str, Any]]:
    try:
        text = (feed_dir(settings) / "feed.txt").read_text(encoding="utf-8")
    except OSError:
        return set(), set(), {}
    urls, hosts = parse_feed(text)
    return urls, hosts, meta(settings)


def match(indicator: str, kind: str, urls: set[str], hosts: set[str], m: dict[str, Any]) -> dict[str, Any] | None:
    if kind not in KINDS:
        return None
    if kind == "url":
        host = (urlsplit(indicator).hostname or "").lower()
        if indicator in urls:
            return verdict(indicator, CID, "malicious", {"match": "url"}, m.get("fetched_at"), kind, True)
    else:
        host = indicator.lower().rstrip(".")
    if host and host in hosts:
        return verdict(indicator, CID, "suspicious", {"match": "host"}, m.get("fetched_at"), kind, True)
    return verdict(indicator, CID, "unknown", {"match": None, "feed_entries": len(urls)}, m.get("fetched_at"),
                   kind, True)
