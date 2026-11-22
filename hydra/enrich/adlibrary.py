"""Ad-library connector for the scheduler bonus task: find suspicious ads, turn them into incidents.

    rules = Rules(search_terms=["inwestycja"], keywords=["zarobek"], known_hosts={"scam-host.example"})
    res = search(rules)                 # SearchResult(status, message_pl, ads=[AdRecord, ...])
    inc = to_incident(res.ads[0], out_dir)   # ResolvedIncident with ad.json / creative.txt / urls.txt artifacts

Sources:
1. Meta Ad Library API `GET https://graph.facebook.com/<ver>/ads_archive` (search_terms, ad_reached_countries,
   ad_active_status, ad_delivery_date_min/max, fields). Token from env `META_AD_LIBRARY_TOKEN` (or the older
   `HYDRA_META_TOKEN`, or the file `~/.config/hydra/meta.env`); it is never logged, cached or persisted (cache
   keys and stored paging/snapshot URLs are redacted). Live calls need
   `Settings.enrich` (HYDRA_ENRICH=1); otherwise cached responses are replayed. Without a token and without a
   cache the result is `unavailable` with a Polish explanation of how to obtain access. `page_through` follows
   the `after` cursor iteratively (one cache entry per page), tries each page 3 times with 1/2/4 s backoff and
   returns a token-free resume cursor on failure; the scheduler adds watermarks, page-id sweeps and dedup.
   Behaviour studied from AdDownloader (GPL-3.0) via docs/cleanroom/addownloader.md; independent implementation.
2. Manual import of what an analyst downloaded: Ad Library CSV/JSON exports, API-shaped JSON, a saved
   ad_snapshot HTML page, a screenshot, and yt-dlp `.info.json` of an Ad Library video (incident-5 format).

Rule engine (`evaluate`): keyword / celebrity / invented platform-name lists, delivery-date window, link-URL host
or URL label skeleton matching known campaign infrastructure, and a new-domain age below N days (RDAP, replay
cache). Each match is a named reason so the UI can show why an ad was flagged.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.contracts import REDACTED, EnrichmentRecord, Incident, IncidentArtifact
from hydra.enrich.client import DELAY, Enricher, Response, record_key
from hydra.enrich.domains import redact_text, redact_url, registrable_domain, url_label_skeleton
from hydra.meta.contract import GRAPH_HOST, GRAPH_VERSION_DEFAULT

GRAPH = f"{GRAPH_HOST}/{GRAPH_VERSION_DEFAULT}/ads_archive"
# META-COLLECT (docs/integrations/META_API_CONTRACT.md, 2026-11-22): `ad_creative_link_urls` is not an ArchivedAd
# field in the current reference and is no longer requested (an unknown field fails the whole query); `_from_api`
# still reads it from older cached answers.
FIELDS = (
    "id",
    "ad_snapshot_url",
    "page_id",
    "page_name",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_titles",
    "ad_creative_link_descriptions",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "publisher_platforms",
    "languages",
)
SOURCE = "meta_adlib"
DELAY.setdefault(SOURCE, 2.0)
RATE_LIMIT_CODES = {1, 2, 4, 17, 32, 341, 613, 80004}  # Graph API transient / throttling error codes
RETRY_BACKOFF_S = (1.0, 2.0, 4.0)  # one entry per attempt: wait 2**attempt s after each failed attempt
PAGE_ID_BATCH = 10  # search_page_ids accepts at most 10 page ids per request

MESSAGE_NO_TOKEN_PL = (
    "Meta Ad Library API jest niedostępne: brak tokenu (zmienna META_AD_LIBRARY_TOKEN, starsza nazwa HYDRA_META_TOKEN, "
    "albo plik ~/.config/hydra/meta.env z linią META_AD_LIBRARY_TOKEN=...). Aby uzyskać dostęp: "
    "1) załóż konto Meta for Developers (developers.facebook.com) i utwórz aplikację; "
    "2) potwierdź tożsamość i lokalizację na facebook.com/ID (wymagane dla Ad Library API); "
    "3) zaakceptuj warunki Ad Library API (facebook.com/ads/library/api) i wygeneruj token użytkownika "
    "w Graph API Explorer; 4) ustaw META_AD_LIBRARY_TOKEN oraz HYDRA_ENRICH=1 i HYDRA_OFFLINE=0. "
    "Bez tokenu możesz ręcznie pobrać "
    "eksport z facebook.com/ads/library (CSV/JSON), zapisać stronę reklamy lub zrzut ekranu i zaimportować go "
    "(import ręczny)."
)
MESSAGE_OFFLINE_PL = (
    "Wyszukiwanie w Meta Ad Library wymaga trybu online (HYDRA_ENRICH=1); "
    "brak zapisanej odpowiedzi w pamięci podręcznej dla tego zapytania."
)

# Default lexicon: generic scam motifs (investment / health / gambling bait). Callers add campaign-specific
# celebrity names and invented platform names from known campaigns.
DEFAULT_KEYWORDS = (
    "zarabiaj",
    "zarobek",
    "zarobić",
    "pasywny dochód",
    "inwestycj",
    "platforma inwestycyjna",
    "kryptowalut",
    "bitcoin",
    "zysk",
    "gwarantowany",
    "bez ryzyka",
    "tylko dziś",
    "ostatnie sztuki",
    "wywiad usunięty",
    "skandal",
    "wyznał",
    "tajemnica",
    "lekarze",
    "cudowny",
    "refundacja",
    "nfz",
    "rząd",
    "ministerstwo",
    "emerytur",
    "kasyno",
    "bonus",
    "darmowe spiny",
    "orlen",
    "pkn",
    "baltic pipe",
    "inpost",
    "pekao",
)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")


@dataclass
class AdRecord:
    ad_id: str
    source: str  # meta_api | manual_csv | manual_json | ytdlp_info | snapshot_html | screenshot
    page_id: str | None = None
    page_name: str | None = None
    bodies: list[str] = field(default_factory=list)
    link_urls: list[str] = field(default_factory=list)
    link_captions: list[str] = field(default_factory=list)
    link_titles: list[str] = field(default_factory=list)
    snapshot_url: str | None = None
    delivery_start: str | None = None
    delivery_stop: str | None = None
    platforms: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    file_path: str | None = None  # manual import: the downloaded file
    fetched_at: str | None = None
    enrichment_key: str | None = None
    reasons: list[str] = field(default_factory=list)
    moderation: str | None = None  # removed_policy_violation: platform-moderation fact, never a linking gene

    def text(self) -> str:
        return "\n".join(
            [*(self.bodies or []), *(self.link_titles or []), *(self.link_captions or []), self.page_name or ""]
        ).strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Rules:
    search_terms: list[str] = field(default_factory=list)  # API query terms (one request per term)
    countries: list[str] = field(default_factory=lambda: ["PL"])
    active_status: str = "ALL"  # ACTIVE | INACTIVE | ALL
    date_min: str | None = None  # YYYY-MM-DD
    date_max: str | None = None
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    celebrities: list[str] = field(default_factory=list)
    platform_names: list[str] = field(default_factory=list)  # invented brands of known campaigns
    known_hosts: set[str] = field(default_factory=set)  # hosts / registrable domains of known campaigns
    known_skeletons: set[str] = field(default_factory=set)  # URL label skeletons of known campaigns
    max_domain_age_days: int | None = 30
    min_reasons: int = 1
    import_paths: list[Path] = field(default_factory=list)  # manual downloads to include
    max_pages: int = 5
    page_size: int = 100
    search_type: str | None = None  # None: KEYWORD_EXACT_PHRASE for multi-word terms, else KEYWORD_UNORDERED


@dataclass
class SearchResult:
    status: str  # ok | unavailable | partial
    message_pl: str | None
    ads: list[AdRecord]
    records: list[EnrichmentRecord] = field(default_factory=list)


# ------------------------------------------------------------------------------------- Meta API


HttpGet = Callable[[str, dict[str, str]], tuple[int, Any]]


def _urllib_get(url: str, params: dict[str, str]) -> tuple[int, Any]:
    """Default HTTP layer of the legacy paths (hint lookup, `job_adlibrary_pull`): the shared META-COLLECT transport
    (`hydra.meta.transport.MetaTransport`: offline kill switch, HYDRA's hourly budget, provider back-off, netguard,
    audit, token scrubbing). A deferral (budget, back-off, offline, no token) is final for this page (a 4xx-shaped
    answer), so the legacy retry loop never hammers Meta."""
    from hydra.config import get_settings
    from hydra.meta.transport import MetaError, MetaTransport

    p = {k: v for k, v in params.items() if k != "access_token"}
    tr = MetaTransport(get_settings(), None, require_enabled=False)
    try:
        r = tr.get(p, url=url)
    except MetaError as e:
        if e.status == "transient":
            return int(e.http_status or 500), {"error": {"code": e.code, "message": e.message_pl}}
        return 400, {"error": {"code": f"hydra:{e.status}", "message": e.message_pl}}
    return 200, r.data


TOKEN_VARS = ("META_AD_LIBRARY_TOKEN", "HYDRA_META_TOKEN")  # first set wins; HYDRA_META_TOKEN kept for old setups


def token_file() -> Path:
    """`~/.config/hydra/meta.env` (KEY=VALUE lines); HYDRA_META_ENV_FILE overrides the path (tests, Docker)."""
    return Path(os.environ.get("HYDRA_META_ENV_FILE") or Path.home() / ".config" / "hydra" / "meta.env")


def _token() -> str | None:
    """Ad Library user token from the single provider `hydra.integrations.secrets.meta_token` (environment, then
    the connector file: `token_file()` by default, `$HYDRA_SECRETS_DIR/meta-adlibrary.env` when that is set).
    Never logged, cached or returned."""
    from hydra.integrations.secrets import meta_token

    return meta_token()


def token_configured() -> bool:
    return _token() is not None


def _scrub(obj: Any, token: str | None) -> Any:
    """Remove the token (and any access_token=) from everything that is cached or returned."""
    if isinstance(obj, dict):
        return {k: _scrub(v, token) for k, v in obj.items() if k != "access_token"}
    if isinstance(obj, list):
        return [_scrub(v, token) for v in obj]
    if isinstance(obj, str):
        s = obj.replace(token, REDACTED) if token else obj
        if "access_token=" in s:
            s = re.sub(r"access_token=[^&\s\"']+", f"access_token={REDACTED}", s)
        return s
    return obj


def _query_key(params: dict[str, str]) -> str:
    """Cache key: a readable label plus sha256 of the full sorted parameter set (never the token). The hash keeps
    keys unique after file-name sanitising/truncation and independent of filesystem case sensitivity."""
    full = "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "access_token")
    label = params.get("search_terms") or "pages-" + params.get("search_page_ids", "")
    return f"{label[:60]}-{hashlib.sha256(full.encode()).hexdigest()[:32]}"


def _from_api(row: dict[str, Any], fetched_at: str | None, key: str | None) -> AdRecord:
    def lst(k: str) -> list[str]:
        v = row.get(k) or []
        return [str(x) for x in (v if isinstance(v, list) else [v])]

    return AdRecord(
        ad_id=str(row.get("id")),
        source="meta_api",
        page_id=row.get("page_id"),
        page_name=row.get("page_name"),
        bodies=lst("ad_creative_bodies"),
        link_urls=lst("ad_creative_link_urls"),
        link_captions=lst("ad_creative_link_captions"),
        link_titles=lst("ad_creative_link_titles"),
        snapshot_url=redact_url(str(row["ad_snapshot_url"])) if row.get("ad_snapshot_url") else None,
        delivery_start=row.get("ad_delivery_start_time"),
        delivery_stop=row.get("ad_delivery_stop_time"),
        platforms=lst("publisher_platforms"),
        languages=lst("languages"),
        fetched_at=fetched_at,
        enrichment_key=key,
    )


def search_type_for(term: str) -> str:
    """Multi-word names are searched as an exact phrase, single words as unordered keywords."""
    return "KEYWORD_EXACT_PHRASE" if len(term.split()) > 1 else "KEYWORD_UNORDERED"


def body_key(page_id: str | None, bodies: Iterable[str]) -> str | None:
    """Dedup key (page id, sha256 of the case/diacritic/whitespace-normalised body); None for text-less ads."""
    norm = " ".join(_fold(" ".join(bodies)).split())
    return f"{page_id or ''}:{hashlib.sha256(norm.encode()).hexdigest()}" if norm else None


def ads_archive_params(rules: Rules, *, term: str | None = None, page_ids: Iterable[str] = ()) -> dict[str, str]:
    """Full ads_archive parameter set for one query (a keyword term and/or <= 10 page ids); never the token."""
    params = {
        "ad_reached_countries": json.dumps(rules.countries),
        "ad_active_status": rules.active_status,
        "fields": ",".join(FIELDS),
        "limit": str(rules.page_size),
        "ad_type": "ALL",
    }
    ids = [str(i) for i in page_ids][:PAGE_ID_BATCH]
    if ids:
        params["search_page_ids"] = ",".join(ids)
    if term is not None:
        params["search_terms"] = term
        if term.strip():
            params["search_type"] = rules.search_type or search_type_for(term)
    if rules.date_min:
        params["ad_delivery_date_min"] = rules.date_min
    if rules.date_max:
        params["ad_delivery_date_max"] = rules.date_max
    return params


@dataclass
class PageRun:
    status: str  # ok | partial (a page failed after all retries, or the replay cache ran out) | unavailable
    ads: list[AdRecord]  # in page order, not deduplicated
    pages: int  # pages read in this call (cache or live)
    resume: dict[str, Any] | None = None  # {"page": n, "after": cursor} of the failed page (token-free)
    error: str | None = None
    records: list[EnrichmentRecord] = field(default_factory=list)


def page_through(
    params: dict[str, str],
    *,
    enricher: Enricher,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_pages: int = 5,
    start_page: int = 0,
    after: str | None = None,
) -> PageRun:
    """Follow the `after` cursor iteratively from (start_page, after) up to max_pages. Every page is one cache
    entry, so a run is replayable offline. Live only when the enricher is live and a token is set."""
    token = _token()
    get = http_get or _urllib_get
    run = PageRun("ok", [], 0)
    page = start_page
    while page < max_pages:
        p = {**params, "after": after} if after else dict(params)

        def fetch(p: dict[str, str] = p) -> tuple[str | None, str, Any]:
            return _fetch_page(get, p, token, sleep)

        resp: Response = enricher.get(SOURCE, _query_key(p), fetch if (token and enricher.live) else None)
        data = resp.data if isinstance(resp.data, dict) else {}
        rows = [r for r in data.get("data") or [] if isinstance(r, dict) and r.get("id")]
        run.records.append(resp.record({"page": page, "n": len(rows)}))
        if not resp.ok:
            miss = resp.status == "unavailable"  # replay cache miss, no live call made
            run.status = "unavailable" if miss and page == start_page else "partial"
            if not miss:
                run.error, run.resume = resp.status, {"page": page, "after": after}
            break
        run.pages += 1
        run.ads.extend(_from_api(r, resp.fetched_at, resp.key) for r in rows)
        paging = data.get("paging") or {}
        after = (paging.get("cursors") or {}).get("after")
        page += 1
        if not rows or not after or not paging.get("next"):
            break
    return run


def meta_search(
    rules: Rules,
    *,
    enricher: Enricher | None = None,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchResult:
    """Query ads_archive for each search term (paged), cached per page; token never stored."""
    s = settings or Settings()
    enr = enricher or Enricher(settings=s)
    ads: dict[str, AdRecord] = {}
    records: list[EnrichmentRecord] = []
    missing = 0
    for term in rules.search_terms or [""]:
        params = ads_archive_params(rules, term=term)
        run = page_through(params, enricher=enr, http_get=http_get, sleep=sleep, max_pages=rules.max_pages)
        records.extend(run.records)
        missing += run.status != "ok"
        for ad in run.ads:
            ads.setdefault(ad.ad_id, ad)
    if not ads and missing:
        msg = MESSAGE_NO_TOKEN_PL if not _token() else MESSAGE_OFFLINE_PL
        return SearchResult("unavailable", msg, [], records)
    return SearchResult("partial" if missing else "ok", None, list(ads.values()), records)


def _fetch_page(
    get: HttpGet, params: dict[str, str], token: str | None, sleep: Callable[[float], None]
) -> tuple[str | None, str, Any]:
    """One page, up to len(RETRY_BACKOFF_S) attempts, waiting 1, 2, 4 s after each failed attempt (exception,
    HTTP 5xx, 429 or a Graph throttling code). Other 4xx errors are final. Nothing returned holds the token."""
    url = GRAPH + "?" + urllib.parse.urlencode(params)
    last = "error:noresp"
    for wait in RETRY_BACKOFF_S:
        try:
            status, data = get(GRAPH, {**params, "access_token": token or ""})
        except Exception as exc:  # noqa: BLE001 — transport failure: retry; only the type name is kept
            status, data, last = 0, None, f"error:{type(exc).__name__}"
        else:
            err = data.get("error") if isinstance(data, dict) else None
            err = err if isinstance(err, dict) else {}
            if status == 200 and not err and isinstance(data, dict):
                return url, "200", _scrub(data, token)
            code = err.get("code")
            last = f"error:http{status}:{code}:{_scrub(redact_text(str(err.get('message', '')))[:120], token)}"
            if 400 <= status < 500 and status != 429 and code not in RATE_LIMIT_CODES:
                return url, last, None
        sleep(wait)
    return url, last, None


# ------------------------------------------------------------------------------------- snapshot status

_REMOVED_RE = re.compile(r"\b(removed|taken down|usunieta|usunieto|usunelismy)\b")
_STANDARDS_RE = re.compile(r"advertising (standards|policies)|standard\w* reklam|zasad\w* reklam")


def snapshot_status(html: str) -> str:
    """Moderation state from a saved ad snapshot page (pure: no browser, no fetch): `removed_policy_violation`
    when the PL or EN banner says the ad was removed for not following the advertising standards, otherwise
    `no_media` (text-only, or media not checked). A platform-moderation fact, never a linking gene."""
    text = re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", html or "", flags=re.S | re.I)
    text = " ".join(_fold(text.replace("’", "'")).split())
    return "removed_policy_violation" if _REMOVED_RE.search(text) and _STANDARDS_RE.search(text) else "no_media"


# ------------------------------------------------------------------------------------- manual import

_ID_KEYS = ("ad_archive_id", "library id", "library_id", "id", "ad id", "identyfikator biblioteki")
_COLS = {
    "page_name": ("page_name", "page name", "nazwa strony", "advertiser"),
    "page_id": ("page_id", "page id", "identyfikator strony"),
    "bodies": ("ad_creative_bodies", "ad_creative_body", "ad creative body", "ad text", "tekst reklamy", "body"),
    "link_urls": ("ad_creative_link_urls", "ad_creative_link_url", "link url", "link_url", "url"),
    "link_titles": ("ad_creative_link_titles", "ad_creative_link_title", "link title", "title"),
    "link_captions": ("ad_creative_link_captions", "ad_creative_link_caption", "link caption", "caption"),
    "snapshot_url": ("ad_snapshot_url", "ad snapshot url", "snapshot"),
    "delivery_start": ("ad_delivery_start_time", "ad delivery start time", "start date", "data rozpoczęcia"),
    "delivery_stop": ("ad_delivery_stop_time", "ad delivery stop time", "end date"),
    "platforms": ("publisher_platforms", "platforms", "platformy"),
}
_LIST_FIELDS = {"bodies", "link_urls", "link_titles", "link_captions", "platforms"}


def _row_to_ad(row: dict[str, Any], source: str, path: Path) -> AdRecord | None:
    low = {str(k).strip().lower(): v for k, v in row.items()}
    ad_id = next((str(low[k]) for k in _ID_KEYS if low.get(k) not in (None, "")), None)
    if not ad_id:
        return None
    ad = AdRecord(ad_id=ad_id, source=source, file_path=str(path))
    for attr, keys in _COLS.items():
        v = next((low[k] for k in keys if low.get(k) not in (None, "", [])), None)
        if v is None:
            continue
        if attr in _LIST_FIELDS:
            vals = v if isinstance(v, list) else [x for x in re.split(r"\s*\|\s*|\n", str(v)) if x]
            setattr(ad, attr, [str(x) for x in vals])
        else:
            setattr(ad, attr, str(v))
    if ad.snapshot_url:
        ad.snapshot_url = redact_url(ad.snapshot_url)
    return ad


def import_file(path: Path) -> list[AdRecord]:
    """Parse one downloaded file into AdRecords (never raises; unknown files give [])."""
    path = Path(path)
    name = path.name.lower()
    try:
        if name.endswith(".info.json"):
            return [r for r in [_from_ytdlp(json.loads(path.read_text(errors="replace")), path)] if r]
        if name.endswith(".json"):
            j = json.loads(path.read_text(errors="replace"))
            rows = j.get("data", j.get("ads", [])) if isinstance(j, dict) else j
            return [a for r in rows if isinstance(r, dict) and (a := _row_to_ad(r, "manual_json", path))]
        if name.endswith((".csv", ".tsv")):
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
            return [
                a
                for r in csv.DictReader(io.StringIO(text), dialect=dialect)
                if (a := _row_to_ad(r, "manual_csv", path))
            ]
        if name.endswith((".html", ".htm", ".mhtml")):
            return [r for r in [_from_snapshot_html(path)] if r]
        if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
            m = re.search(r"(\d{13,17})", path.name)
            return [
                AdRecord(ad_id=m.group(1) if m else f"img-{path.stem[:40]}", source="screenshot", file_path=str(path))
            ]
    except (OSError, ValueError, csv.Error, UnicodeError):
        return []
    return []


def _from_ytdlp(j: dict[str, Any], path: Path) -> AdRecord | None:
    """yt-dlp `facebook:ads` info.json (Ad Library video): library id, page, title/description, start date."""
    if not isinstance(j, dict):
        return None
    lib = str(j.get("playlist_id") or str(j.get("id", "")).split("_")[0] or "")
    if not lib:
        return None
    up = j.get("upload_date") or j.get("timestamp")
    start = None
    if isinstance(up, str) and re.fullmatch(r"\d{8}", up):
        start = f"{up[:4]}-{up[4:6]}-{up[6:]}"
    elif isinstance(up, int | float):
        start = datetime.fromtimestamp(up, UTC).date().isoformat()
    bodies = [x for x in (j.get("description"), j.get("title") or j.get("playlist_title")) if x]
    return AdRecord(
        ad_id=lib,
        source="ytdlp_info",
        page_id=str(j.get("playlist_uploader_id") or "") or None,
        page_name=j.get("playlist_uploader") or j.get("uploader"),
        bodies=[str(b) for b in bodies],
        snapshot_url=redact_url(str(j.get("webpage_url") or f"https://www.facebook.com/ads/library/?id={lib}")),
        delivery_start=start,
        file_path=str(path),
    )


def _from_snapshot_html(path: Path) -> AdRecord | None:
    from bs4 import BeautifulSoup

    raw = path.read_bytes().decode("utf-8", "replace")
    soup = BeautifulSoup(raw, "lxml")
    text = soup.get_text("\n", strip=True)
    m = re.search(r"(?:Library ID|Identyfikator biblioteki|id=)\s*:?\s*(\d{13,17})", text + str(soup)[:200000])
    ad_id = m.group(1) if m else f"html-{path.stem[:40]}"
    links = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "l.facebook.com/l.php" in href:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("u")
            href = q[0] if q else href
        if href.startswith("http") and "facebook.com" not in href:
            links.append(href)
    sm = re.search(r"(?:Started running on|Data rozpoczęcia emisji:?|Rozpoczęcie emisji)\s*([^\n]{6,30})", text)
    return AdRecord(
        ad_id=ad_id,
        source="snapshot_html",
        bodies=[text[:5000]] if text else [],
        link_urls=sorted(set(links))[:20],
        delivery_start=sm.group(1).strip() if sm else None,
        file_path=str(path),
        moderation="removed_policy_violation" if snapshot_status(raw) == "removed_policy_violation" else None,
    )


# ------------------------------------------------------------------------------------- rules


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip()[:25].replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d


def _hosts(urls: Iterable[str]) -> list[str]:
    out = []
    for u in urls:
        u = u if "://" in u else "https://" + u
        h = (urllib.parse.urlsplit(u).hostname or "").lower()
        if h:
            out.append(h)
    return out


def evaluate(ad: AdRecord, rules: Rules, *, enricher: Enricher | None = None) -> list[str]:
    """Named reasons why the ad is suspicious under `rules` (empty list = no rule fired)."""
    reasons: list[str] = []
    text = _fold(ad.text())
    for kind, words in (
        ("keyword", rules.keywords),
        ("celebrity", rules.celebrities),
        ("platform_name", rules.platform_names),
    ):
        for w in words:
            fw = _fold(w)
            if fw and re.search(r"(?<!\w)" + re.escape(fw), text):
                reasons.append(f"{kind}:{w}")
    start, lo, hi = _parse_date(ad.delivery_start), _parse_date(rules.date_min), _parse_date(rules.date_max)
    if start and (lo or hi) and (not lo or start >= lo) and (not hi or start <= hi):
        reasons.append(f"date_window:{start.date().isoformat()}")
    hosts = _hosts(ad.link_urls + ad.link_captions)
    for h in hosts:
        rd = registrable_domain(h)
        if h in rules.known_hosts or (rd and rd in rules.known_hosts):
            reasons.append(f"known_host:{h}")
        sk = url_label_skeleton(h)
        if sk and sk in rules.known_skeletons:
            reasons.append(f"url_skeleton:{sk}")
    if rules.max_domain_age_days is not None and hosts:
        from hydra.enrich import rdap
        from hydra.enrich.domains import icann_domain, is_platform

        enr = enricher or Enricher()
        ref = start or datetime.now(UTC)
        for d in sorted({icann_domain(h) for h in hosts if not is_platform(h)} - {None}):
            _, summ = rdap.domain(enr, d)  # replay unless live is enabled
            reg = _parse_date(summ.get("registration"))
            if reg is not None:
                age = (ref - reg).total_seconds() / 86400
                if age < rules.max_domain_age_days:
                    reasons.append(f"new_domain:{d}:{age:.0f}d")
    return sorted(set(reasons))


def search(
    rules: Rules,
    *,
    enricher: Enricher | None = None,
    settings: Settings | None = None,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchResult:
    """Ad Library API (if possible) + manual imports, filtered by the rule engine (>= rules.min_reasons)."""
    s = settings or Settings()
    enr = enricher or Enricher(settings=s)
    res = (
        meta_search(rules, enricher=enr, settings=s, http_get=http_get, sleep=sleep)
        if rules.search_terms
        else SearchResult("ok", None, [])
    )
    pool = {a.ad_id: a for a in res.ads}
    for p in rules.import_paths:
        for a in import_file(Path(p)):
            pool.setdefault(a.ad_id, a)
    flagged = []
    for a in pool.values():
        a.reasons = evaluate(a, rules, enricher=enr)
        if len(a.reasons) >= rules.min_reasons:
            flagged.append(a)
    status = res.status if not rules.import_paths else ("ok" if pool else res.status)
    return SearchResult(status, res.message_pl, flagged, res.records)


def ad_summary(ad: AdRecord) -> dict[str, Any]:
    """What the UI shows for one ad (redacted text, link hosts, named rule reasons); never the token."""
    return {
        "ad_id": ad.ad_id, "page_id": ad.page_id, "page_name": ad.page_name,
        "text": redact_text(ad.text())[:300], "link_hosts": sorted(set(_hosts(ad.link_urls)))[:10],
        "delivery_start": ad.delivery_start, "snapshot_url": ad.snapshot_url, "reasons": ad.reasons,
        "moderation": ad.moderation, "source": ad.source,
    }


def hint_lookup(
    hint: str,
    settings: Settings | None = None,
    *,
    enricher: Enricher | None = None,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] = time.sleep,
    limit: int = 20,
) -> dict[str, Any]:
    """Ad Library part of the product-name / hint search (`pipeline.search_hint`).

    One page of up to 50 ads for the hint as a search term (exact phrase for several words), every ad scored by the
    rule engine (default lexicon + the hint; no RDAP calls here). Live only with a token and live enrichment
    (HYDRA_ENRICH=1, HYDRA_OFFLINE=0); otherwise the cached answer for the same query is replayed. Status:
    `ok` (mode live | replay), `not_configured` (no token and nothing cached), `offline` (token, offline, nothing
    cached), `partial` (the API failed after retries; error kept)."""
    from hydra.enrich.client import live_enabled

    s = settings or Settings()
    enr = enricher or Enricher(settings=s)
    if enricher is None and not live_enabled(s):
        enr.live = False
    term = " ".join(hint.split())
    has_token = token_configured()
    base: dict[str, Any] = {"token_configured": has_token, "query": term, "ads": [], "n_ads": 0}
    if not term:
        return {**base, "status": "idle", "note": "pusta fraza"}
    rules = Rules(search_terms=[term], keywords=[*DEFAULT_KEYWORDS, term], max_pages=1, page_size=50,
                  max_domain_age_days=None, min_reasons=0)
    run = page_through(ads_archive_params(rules, term=term), enricher=enr, http_get=http_get, sleep=sleep,
                       max_pages=1)
    mode = "live" if (enr.live and has_token) else "replay"
    if run.status == "unavailable":
        if not has_token:
            return {**base, "status": "not_configured", "mode": mode, "note": MESSAGE_NO_TOKEN_PL}
        return {**base, "status": "offline", "mode": mode, "note": MESSAGE_OFFLINE_PL}
    ads = {a.ad_id: a for a in run.ads}
    for a in ads.values():
        a.reasons = evaluate(a, rules, enricher=enr)
    ranked = sorted(ads.values(), key=lambda a: (-len(a.reasons), a.ad_id))
    out = {**base, "status": "ok" if run.status == "ok" else "partial", "mode": mode, "n_ads": len(ads),
           "ads": [ad_summary(a) for a in ranked[:limit]],
           "fetched_at": max((a.fetched_at or "" for a in ads.values()), default=None) or None}
    if run.error:
        out["note"] = _scrub(run.error, _token())
    return out


# ------------------------------------------------------------------------------------- incident


def to_incident(ad: AdRecord, out_dir: Path) -> Any:
    """Write the ad as an incident folder (ad.json, creative.txt, urls.txt, the downloaded file) and return a
    hydra.genome.base.ResolvedIncident (Incident + artifacts) for the pipeline. `out_dir` is created."""
    import hashlib

    from hydra.genome.base import ResolvedArtifact, ResolvedIncident

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: list[tuple[str, str, str]] = []  # (rel, role, mime)

    def put(rel: str, data: bytes, role: str, mime: str) -> None:
        (out / rel).write_bytes(data)
        files.append((rel, role, mime))

    put(
        "ad.json",
        json.dumps(ad.to_dict(), ensure_ascii=False, indent=1).encode(),
        "platform_metadata",
        "application/json",
    )
    if ad.text():
        put("creative.txt", ad.text().encode(), "text", "text/plain")
    urls = [u for u in [*ad.link_urls, ad.snapshot_url or ""] if u.startswith("http")]
    if urls:
        put("urls.txt", "\n".join(redact_url(u) for u in urls).encode(), "url", "text/plain")
    if ad.file_path and Path(ad.file_path).is_file():
        src = Path(ad.file_path)
        low = src.name.lower()
        role = (
            "ad_creative"
            if low.endswith((".png", ".jpg", ".jpeg", ".webp"))
            else "landing_html"
            if low.endswith((".html", ".htm", ".mhtml"))
            else "platform_metadata"
        )
        dst = out / ("source_" + src.name)
        shutil.copyfile(src, dst)
        files.append((dst.name, role, ""))
    arts = []
    for rel, role, mime in files:
        p = out / rel
        b = p.read_bytes()
        meta = IncidentArtifact(
            artifact_id=hashlib.sha256(b).hexdigest(),
            role=role,
            path_in_incident=rel,
            mime=mime,
            size=len(b),
            meta={"ad_id": ad.ad_id, "source": ad.source},
        )
        arts.append(ResolvedArtifact(meta, p))
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", ad.ad_id)[:60]
    inc = Incident(
        id=f"adlib-{safe}",
        source=f"adlibrary:{ad.source}",
        source_ref=ad.snapshot_url or ad.file_path or ad.ad_id,
        title=(ad.link_titles or ad.bodies or [None])[0],
        observed_at=ad.delivery_start,
        artifacts=[a.meta for a in arts],
        meta={
            "ad_id": ad.ad_id,
            "page_id": ad.page_id,
            "page_name": ad.page_name,
            "reasons": ad.reasons,
            "platforms": ad.platforms,
            "url": next((u for u in ad.link_urls if u.startswith("http")), None),
            "enrichment_key": ad.enrichment_key or record_key(SOURCE, ad.ad_id),
            **({"moderation": ad.moderation} if ad.moderation else {}),
        },
    )
    return ResolvedIncident(inc, arts)
