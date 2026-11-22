"""Enrichment client: one JSON file per (source, query) in a replayable evidence cache (ADR-007).

Cache layout (compatible with the analyst's exploratory cache): `<cache>/<source>/<safe(query)>.json` holding
`{source, key, query, url, fetched_at, status, data}`. Replay (default) reads only the cache; a missing entry is
`unavailable`, never an error. Cache root: `Settings.enrich_cache` (HYDRA_ENRICH_CACHE). Live lookups need
`HYDRA_ENRICH=1` (Settings.enrich); they are rate-limited per source, timestamped and cached. Transient failures
go to `<name>.error.json` and are retried on the next live run. The client is reusable by the scheduler
(periodic re-enrichment): `Enricher(max_age_s=...)` re-fetches cached entries older than
that when live and keeps the previous response as `<name>.<fetched_at>.json` history.

Only hostnames and IPs extracted from the material are queried here. Fetching a page given as input is the
separate, opt-in `hydra.enrich.fetch` module.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.contracts import EnrichmentRecord, GeneStatus
from hydra.netguard import UnsafeDestination, check_url, offline

USER_AGENT = "hydra-nexus-enrich/1.0 (research; public registries only)"
# Seconds between live calls per source (politeness; crt.sh is slow and fragile, registries throttle).
DELAY: dict[str, float] = {"rdap": 1.0, "rdap_ip": 1.0, "crtsh": 3.0, "dns": 0.05}

# Statuses that are facts (the lookup worked; the answer may be "does not exist").
FACT_STATUSES = {"200", "ok", "NXDOMAIN", "NODATA", "404"}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def safe_name(query: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", query)[:150]


def record_key(source: str, query: str) -> str:
    return hashlib.sha256(f"{source}|{query}".encode()).hexdigest()


@dataclass
class Response:
    source: str
    query: str
    status: str  # "200", "ok", "NXDOMAIN", "404", "unavailable", "error:..."
    data: Any
    fetched_at: str | None
    cache_path: Path | None
    url: str | None = None

    @property
    def key(self) -> str:
        return record_key(self.source, self.query)

    @property
    def ok(self) -> bool:
        return self.status in FACT_STATUSES and self.data is not None and self.status not in ("404",)

    @property
    def gene_status(self) -> GeneStatus:
        return GeneStatus.OBSERVED if self.status in FACT_STATUSES else GeneStatus.UNAVAILABLE

    def record(self, summary: dict[str, Any] | None = None) -> EnrichmentRecord:
        sha = None
        if self.cache_path and self.cache_path.is_file():
            sha = hashlib.sha256(self.cache_path.read_bytes()).hexdigest()
        return EnrichmentRecord(
            key=self.key,
            source=self.source,
            query=self.query,
            fetched_at=self.fetched_at or now_iso(),
            status=self.gene_status,
            response_sha256=sha,
            cache_path=str(self.cache_path) if self.cache_path else None,
            summary={"status": self.status, **(summary or {})},
        )


def default_cache_dir(settings: Settings | None = None) -> Path:
    """Settings.enrich_cache (HYDRA_ENRICH_CACHE, default <cache_dir>/enrich)."""
    s = settings or Settings()
    assert s.enrich_cache is not None
    return Path(s.enrich_cache)


def live_enabled(settings: Settings | None = None) -> bool:
    """Live lookups need Settings.enrich (HYDRA_ENRICH=1) AND not Settings.offline: HYDRA_OFFLINE=1 is the hard
    kill switch (audit A09). Replay from the cache works either way."""
    s = settings or Settings()
    return bool(s.enrich) and not bool(s.offline)


class Enricher:
    """Cache + replay + polite live fetch. Thread-safe enough for one pipeline process."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        live: bool | None = None,
        settings: Settings | None = None,
        max_age_s: float | None = None,
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir(settings)
        # HYDRA_OFFLINE=1 dominates an explicit live=True (CR-ASTRA-SEC-8).
        self.live = (live_enabled(settings) if live is None else bool(live)) and not offline(settings)
        self.max_age_s = max_age_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.stats = {"hit": 0, "miss": 0, "live": 0, "error": 0}

    def _stale(self, j: dict[str, Any]) -> bool:
        if not (self.live and self.max_age_s):
            return False
        try:
            t = datetime.fromisoformat(str(j.get("fetched_at")))
        except ValueError:
            return True
        return (datetime.now(UTC) - t).total_seconds() > self.max_age_s

    def path(self, source: str, query: str) -> Path:
        return self.cache_dir / source / f"{safe_name(query)}.json"

    def get(self, source: str, query: str, fetch: Callable[[], tuple[str | None, str, Any]] | None = None) -> Response:
        p = self.path(source, query)
        if p.is_file():
            try:
                j = json.loads(p.read_text())
                if self._stale(j):
                    stamp = safe_name(str(j.get("fetched_at") or "old"))
                    p.replace(p.with_name(f"{p.stem}.{stamp}.json"))
                    raise ValueError("stale")
                self.stats["hit"] += 1
                return Response(
                    source, query, str(j.get("status")), j.get("data"), j.get("fetched_at"), p, j.get("url")
                )
            except (OSError, ValueError):
                pass
        if not self.live or fetch is None:
            self.stats["miss"] += 1
            return Response(source, query, "unavailable", None, None, None)
        with self._lock:
            wait = DELAY.get(source, 0.5) - (time.monotonic() - self._last.get(source, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[source] = time.monotonic()
        try:
            url, status, data = fetch()
        except Exception as exc:  # noqa: BLE001 — any upstream failure is "unavailable", never a crash
            url, status, data = None, f"error:{type(exc).__name__}:{str(exc)[:120]}", None
        self.stats["live"] += 1
        rec = {
            "source": source,
            "key": safe_name(query),
            "query": query,
            "url": url,
            "fetched_at": now_iso(),
            "status": status,
            "data": data,
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        if str(status) in FACT_STATUSES:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
            tmp.replace(p)
            p.with_name(p.stem + ".error.json").unlink(missing_ok=True)
            return Response(source, query, str(status), data, rec["fetched_at"], p, url)
        self.stats["error"] += 1
        p.with_name(p.stem + ".error.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        return Response(source, query, str(status), None, rec["fetched_at"], None, url)


MAX_REGISTRY_REDIRECTS = 5


class _VettedRedirects(dict):
    """Redirect bookkeeping that vets every hop (CR-ASTRA-SEC-9).

    urllib's HTTPRedirectHandler consults `request.redirect_dict.get(newurl)` for each redirect before it opens
    the new URL, and hands the same dict to the follow-up request. Vetting there applies the SSRF policy
    (`hydra.netguard.check_url`: http/https only, no local names, no non-public literal IPs) to every hop with
    whichever opener is installed, and bounds the chain to MAX_REGISTRY_REDIRECTS hops."""

    def get(self, url, default=None):  # type: ignore[override]
        try:
            check_url(str(url))
        except UnsafeDestination as exc:
            raise urllib.error.URLError(f"redirect blocked: {exc}") from exc
        if len(self) >= MAX_REGISTRY_REDIRECTS:
            raise urllib.error.URLError("redirect blocked: too many redirects")
        return super().get(url, default)


def http_json(url: str, timeout: float = 40, tries: int = 2) -> tuple[str | None, str, Any]:
    """GET a JSON document from a public registry / CT service. 404 is a fact (no record). Redirects are
    followed only to public http(s) destinations."""
    last = "noresp"
    try:
        check_url(url)
    except UnsafeDestination as exc:
        return url, f"error:blocked:{exc}"[:200], None
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        req.redirect_dict = _VettedRedirects()  # type: ignore[attr-defined]
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed https endpoints only
                body = r.read(20_000_000)
                try:
                    return r.geturl(), "200", json.loads(body)
                except ValueError:
                    return r.geturl(), "200", {"_text": body[:20000].decode("utf-8", "replace")}
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return url, "404", {}
            last = f"error:http{e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"error:{type(e).__name__}"
            if "redirect blocked" in str(getattr(e, "reason", "")):
                return url, f"error:{getattr(e, 'reason', '')}"[:200], None
        time.sleep(4 * (i + 1))
    return url, last, None
