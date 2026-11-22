"""Static parsing of saved web material (HAR, HTML, MHTML, saved `_files`) shared by the har/dom/url/inf genes.

Parse only: never executes JavaScript and never fetches anything. Each artifact is parsed once per process
(cached by path + size + mtime) because the har, dom, url and inf extractors all read the same files.
"""

from __future__ import annotations

import base64
import email
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.enrich.domains import is_boilerplate, path_skeleton, redact_text, redact_url, valid_host
from hydra.genome.base import ExtractResult, ResolvedArtifact, ResolvedIncident

MAX_TEXT_SCAN = 3_000_000  # bytes of a JS/HTML body scanned with regexes
MAX_FILE_BYTES = 200_000_000  # HAR / HTML / MHTML larger than this are not parsed (gene status unavailable)
FILE_TOO_LARGE = "FILE_TOO_LARGE"

# ------------------------------------------------------------------------------------------- regexes

TRACKER_RX: dict[str, list[re.Pattern[str]]] = {
    "fb_pixel": [
        re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{10,20})"),
        re.compile(r"facebook\.com/tr/?\?(?:[^\"'\s]*&)?id=(\d{10,20})"),
        re.compile(r"[?&](?:pxl|fbpx|fb_pixel|pixel_fb|pixel)=(\d{10,20})"),
        re.compile(r"connect\.facebook\.net/signals/config/(\d{10,20})"),
    ],
    "gtm": [re.compile(r"\b(GTM-[A-Z0-9]{5,9})\b")],
    "ga4": [re.compile(r"\b(G-[A-Z0-9]{8,12})\b")],
    "ua": [re.compile(r"\b(UA-\d{4,10}-\d{1,3})\b")],
    "google_ads": [
        re.compile(r"\b(AW-\d{8,12})\b"),
        re.compile(r"pagead/1p-conversion/(\d{8,12})/"),
        re.compile(r"pagead/viewthroughconversion/(\d{8,12})/"),
    ],
    "yandex_metrica": [
        re.compile(r"ym\(\s*(\d{6,10})\s*,\s*['\"]init"),
        re.compile(r"mc\.yandex\.ru/watch/(\d{6,10})"),
    ],
    "tiktok_pixel": [
        re.compile(r"ttq\.load\(\s*['\"]([A-Z0-9]{15,25})"),
        re.compile(r"analytics\.tiktok\.com/[^\"'\s]*sdkid=([A-Z0-9]{15,25})"),
    ],
    "clarity": [re.compile(r"clarity\.ms/tag/([a-z0-9]{8,12})")],
    "hotjar": [re.compile(r"hjid\s*:\s*(\d{6,9})")],
    "snap_pixel": [re.compile(r"snaptr\(\s*['\"]init['\"]\s*,\s*['\"]([0-9a-f-]{36})")],
}

# Operator/backend identifiers found in bundles, POST bodies and URLs (campaign-level, very rare).
OPID_RX: dict[str, re.Pattern[str]] = {
    "landing_cdn_account": re.compile(r"/_cdn/production/landing-cdn/([^/\\\"']+)/[0-9a-f]{8}-"),
    "landing_cdn_landing": re.compile(r"/_cdn/production/landing-cdn/[^/\\\"']+/([0-9a-f]{8}-[0-9a-f-]{27})"),
    "lander_dir": re.compile(r"/lander/([a-z0-9_-]{6,80})/"),
    "exit_offer_cuuid": re.compile(r"[?&]cuuid=([0-9a-f]{8}-[0-9a-f-]{27})"),
    "exit_offer_suuid": re.compile(r"[?&]suuid=([0-9a-f]{8}-[0-9a-f-]{27})"),
    "form_event_backend": re.compile(r"FORM_EVENT_BACKEND_DOMAIN\s*=\s*['\"]https?://([a-z0-9.-]+)"),
    "action_files": re.compile(r"/_cdn/production/action-files/([0-9a-f]{8}-[0-9a-f-]{27})"),
    "landing_uuid": re.compile(
        r"(?:landed_on_landings_uuid|landing_uuid|landed_on_landings_types_uuid)"
        r"\\?[\"']?\s*[:=]\s*\\?[\"']?([0-9a-f]{8}-[0-9a-f-]{27})"
    ),
    "cloak_sid": re.compile(r"cl_sid\\?[\"']?\s*:\s*\\?[\"']?(\d{3,})"),
    "cloak_domain_id": re.compile(r"\bdomain_id\\?[\"']?\s*:\s*\\?[\"']?(\d{3,})"),
    "team_id": re.compile(r"[?&]team_id=(\d+)"),
    "offer_uuid": re.compile(r"offer_uuid\\?[\"']?\s*(?:value=|[:=])\s*\\?[\"']?([0-9a-f]{8}-[0-9a-f-]{27})"),
    "fb_page_ref": re.compile(r"facebook\.com/(?:profile\.php\?id=)?(\d{12,17})"),
    "postback_host": re.compile(
        r"https?:\\?/\\?/([a-z0-9.-]+)\\?/(?:click|postback|titlepost|lead|order|kei)\w*(?:\.php)?"
    ),
    "crm_host": re.compile(r"https?://([a-z0-9.-]*crm[a-z0-9.-]*)/api", re.I),
}

# Traffic-distribution systems / trackers / lead SDKs recognisable by strings.
TDS_SIGS: list[tuple[str, re.Pattern[str]]] = [
    ("binom", re.compile(r"upd_clickid=|click\.php\?key=|[?&]lpkey=|/click\?event\d+=|cp\.binom\.org", re.I)),
    (
        "keitaro",
        re.compile(
            r"_update_tokens|[?&]_subid=|\bkeitaro\b|/\?_lp=1|\bKTracking\b|[?&]_token=|"
            r"[?&]sub_id_\d+=|\{sub_id_\d+\}",
            re.I,
        ),
    ),
    ("voluum", re.compile(r"voluum|/redirect\?cid=|dtrk\.", re.I)),
    ("bemob", re.compile(r"\bbemob\b", re.I)),
    ("redtrack", re.compile(r"\bredtrack\b|rtkclickid", re.I)),
    ("aio_sdk", re.compile(r"/aio-static/sdk/|/api/v1/trigger/field/", re.I)),
    ("cloaker_fields", re.compile(r"\bcl_sid\b|\bcl_check\b|\bcl_domain\b")),
    ("utm_visit_uuidv7", re.compile(r"[?&]utm_visit=01[0-9a-f]{6}-", re.I)),
    ("everflow", re.compile(r"everflow|/\?oid=\d+&affid=", re.I)),
    ("cake", re.compile(r"/\?a=\d+&c=\d+&s1=", re.I)),
]

CLOAK_RX: dict[str, re.Pattern[str]] = {
    "webdriver_check": re.compile(r"navigator\.webdriver"),
    "devtools_block": re.compile(r"devtools-?detect|keyCode\s*===?\s*123|\bdebugger;", re.I),
    "rightclick_block": re.compile(r"contextmenu['\"]?\s*,\s*function|oncontextmenu\s*=\s*['\"]?return false", re.I),
    "ip_geo_lookup": re.compile(
        r"ipapi\.co|ip-api\.com|ipinfo\.io|ipwho\.is|api\.country\.is|geoplugin\.net", re.I
    ),
    # a referrer *decision* (comparison, search, negation); reading it for analytics is not cloaking (G05 d)
    "referrer_check": re.compile(
        r"document\.referrer\s*(?:===?|!==?|\.(?:indexOf|includes|match|search|startsWith|test)\s*\()"
        r"|!\s*document\.referrer\b|\(\s*document\.referrer\s*\)\s*\{"
    ),
    "timezone_check": re.compile(r"getTimezoneOffset|resolvedOptions\(\)\.timeZone"),
    "history_trap": re.compile(r"onpopstate|addEventListener\(\s*['\"]popstate", re.I),
    "fingerprint": re.compile(r"fingerprintjs|canvas\.toDataURL", re.I),
    # behaviour gates and bot checks (GAPS_PAPERS G05; CrawlPhish / PhishTime taxonomies, own regexes)
    "mouse_gate": re.compile(
        r"addEventListener\(\s*['\"](?:mousemove|mouseover|touchstart|pointermove)['\"]"
    ),
    "alert_gate": re.compile(
        r"\b(?:alert|confirm)\s*\([^)]{0,200}\)[^;]{0,40};?\s*(?:window\.|top\.|document\.)?location"
    ),
    "click_gate": re.compile(
        r"addEventListener\(\s*['\"]click['\"][^;]{0,300}location|onclick\s*=\s*['\"][^'\"]{0,300}location", re.I
    ),
    "cookie_check": re.compile(
        r"document\.cookie\s*\.\s*(?:indexOf|includes|match|search)\s*\(|navigator\.cookieEnabled"
    ),
    "ua_check": re.compile(
        r"navigator\.userAgent[^;\n]{0,200}(?:bot\b|crawl|spider|headless|phantom|lighthouse|facebookexternalhit)"
        r"|(?:bot\b|crawl|spider|headless|phantom|lighthouse|facebookexternalhit)[^;\n]{0,200}navigator\.userAgent",
        re.I,
    ),
    "timer_gate": re.compile(
        r"setTimeout\(\s*(?:function\s*\(\s*\)\s*\{|\(\s*\)\s*=>\s*\{?)[^}]{0,300}location[^}]{0,300}\}?\s*,\s*\d{3,}"
    ),
    "random_gate": re.compile(r"Math\.random\(\)\s*[<>]=?\s*0?\.\d+"),
    "captcha_gate": re.compile(
        r"g-recaptcha|grecaptcha\.|hcaptcha|cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I
    ),
    "random_path_redirect": re.compile(
        r"location(?:\.href)?\s*=[^;\n]{0,160}Math\.random\(\)\.toString\(\s*36\s*\)"
    ),
}
CLOAK_IMPL_WINDOW = 400  # characters around each cloaking match that make up the implementation snippet
_JS_TOKEN = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`|\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|\S")
_JS_KEEP = {  # keywords and Web API names stay; every other identifier becomes `I`, literals `S` / `N`
    "var", "let", "const", "function", "return", "if", "else", "for", "while", "new", "typeof", "true", "false",
    "null", "undefined", "this", "window", "document", "navigator", "location", "href", "replace", "assign",
    "referrer", "cookie", "cookieEnabled", "userAgent", "webdriver", "addEventListener", "setTimeout",
    "setInterval", "Math", "random", "toString", "alert", "confirm", "indexOf", "includes", "match", "test",
    "search", "getTimezoneOffset", "Intl", "DateTimeFormat", "resolvedOptions", "timeZone", "fetch", "then",
    "json", "XMLHttpRequest", "open", "send", "history", "pushState", "onpopstate", "top", "self", "event",
}
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>(.*?)</script>", re.I | re.S)


def cloak_impl_hash(code: str) -> str | None:
    """Masked token stream of the code around cloaking matches (identifiers -> I, strings -> S, numbers -> N):
    renamed variables and different redirect URLs give the same value (GAPS_PAPERS G05)."""
    spans: list[list[int]] = []
    for rx in CLOAK_RX.values():
        for m in rx.finditer(code):
            a, b = max(0, m.start() - CLOAK_IMPL_WINDOW), min(len(code), m.end() + CLOAK_IMPL_WINDOW)
            if spans and a <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
    if not spans:
        return None
    spans.sort()
    toks = []
    for a, b in spans:
        for t in _JS_TOKEN.findall(code[a:b]):
            if t[0] in "'\"`":
                toks.append("S")
            elif t[0].isdigit():
                toks.append("N")
            elif t[0].isalpha() or t[0] in "_$":
                toks.append(t if t in _JS_KEEP else "I")
            else:
                toks.append(t)
    return h16(" ".join(toks)) if len(toks) >= 20 else None

REDIR_RX: list[tuple[str, re.Pattern[str]]] = [
    (
        "js_location",
        re.compile(r"(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*=\s*['\"`]([^'\"`\s]{4,300})"),
    ),
    ("js_location_replace", re.compile(r"location\.(?:replace|assign)\(\s*['\"`]([^'\"`\s]{4,300})")),
    ("window_open", re.compile(r"window\.open\(\s*['\"`]([^'\"`\s]{4,300})")),
]
META_REFRESH = re.compile(
    r"<meta[^>]+http-equiv=['\"]?refresh['\"]?[^>]*content=['\"]?\s*\d+\s*;\s*url=([^'\">\s]+)", re.I
)

ENDPOINT_RX = re.compile(
    r"['\"`]((?:https?://[^'\"`\s]+)?/[A-Za-z0-9_\-./]*(?:api/[A-Za-z0-9_\-./{}]*|lead|order|send|submit|register|"
    r"signup|postback|conversion|thank|success|form)[A-Za-z0-9_\-./]*(?:\.php)?)(?:\?[^'\"`]*)?['\"`]",
    re.I,
)
STATIC_EXT = re.compile(r"\.(css|png|jpe?g|webp|svg|gif|woff2?|ttf|eot|ico|mp4|webm|m3u8|ts|map)$", re.I)

LIB_RX = re.compile(
    r"/(jquery|bootstrap|swiper|embla-carousel|lazysizes|hls\.js|hls|video\.js|vue|react|react-dom|slick|"
    r"owl\.carousel|aos|gsap|intl-tel-input|imask|inputmask|font-?awesome|animate|sweetalert2|axios|lodash|"
    r"moment|alpinejs|htmx|tailwindcss)(?:[.@/-]?(?:min)?)?[@/-]?v?(\d+\.\d+(?:\.\d+)?)",
    re.I,
)
LIB_NAME = re.compile(
    r"^(jquery|bootstrap|swiper|hls|lazysizes|embla|vue|react|font-?awesome|all\.min|fontawesome|gtm|fbevents|"
    r"analytics|gtag|js\?id=|sweetalert2|axios|lodash|moment|slick|owl\.carousel|aos|gsap|intl-tel-input|"
    r"imask|inputmask|animate|signals|events|recaptcha|api\.js|saved_resource)",
    re.I,
)

EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RX = re.compile(r"(?<![\d+])(?:\+48[\s-]?)?\d{3}[\s-]?\d{3}[\s-]?\d{3}(?![\d])")
IBAN_RX = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[0-9A-Z]{4}){3,7}(?:\s?[0-9A-Z]{1,4})?\b")
URL_HOST_RX = re.compile(r"https?:\\?/\\?/([A-Za-z0-9.\-]+)")

CLICK_PARAMS = {
    "fbclid",
    "gclid",
    "ttclid",
    "yclid",
    "msclkid",
    "wbraid",
    "gbraid",
    "clickid",
    "click_id",
    "subid",
    "sub_id",
    "sub1",
    "sub2",
    "sub3",
    "sub4",
    "sub5",
    "campaign_id",
    "cid",
    "_camp_id",
    "land_id",
    "lpkey",
    "_trk",
    "trafficsource",
    "pxl",
    "fbpx",
    "pixel_fb",
    "pixel",
    "ad_id",
    "adset_id",
    "adset_name",
    "ad_name",
    "campaign_name",
    "placement",
    "acc",
    "key",
    "offer",
    "offer_id",
    "aff_id",
    "affid",
    "affiliate_id",
    "aff_sub",
    "aff_sub2",
    "source",
    "s1",
    "s2",
    "s3",
    "s4",
    "s5",
    "x_id",
    "sid",
    "user_id",
    "team_id",
    "network",
    "landing_id",
    "media_type",
    "site_source_name",
    # ad-platform parameters (SCAMLINKS 2026-11-21): names are platform defaults; values handled by AD_ID_PARAMS
    "gad_source",
    "gad_campaignid",
    "dclid",
    "aatid",
    "utm_id",
    "adgroup_id",
    "creative_id",
    "twclid",
    "li_fat_id",
    "epik",
    "sccid",
}
# Parameters whose *values* identify a campaign / account (not a single click). utm_source/utm_medium are
# excluded: their values (ig, paid, cpc) are platform defaults shared by unrelated campaigns.
CAMPAIGN_VALUE_PARAMS = {
    "_camp_id",
    "campaign_id",
    "cid",
    "land_id",
    "landing_id",
    "acc",
    "user_id",
    "team_id",
    "trafficsource",
    "_trk",
    "utm_campaign",
    "utm_content",
    "key",
    "lpkey",
    "offer",
    "offer_id",
    "aff_id",
    "affid",
    "affiliate_id",
    "network",
    "sub1",
    "sub2",
    "s1",
    "pxl",
    "fbpx",
    "pixel_fb",
    "pixel",
    "campaign_name",
    "adset_name",
    "theme",
    "tracker",
}

# ------------------------------------------------------------------------------------------- ad platforms
# SCAMLINKS (2026-11-21). Per-click ids (gclid, fbclid, ttclid, ...) differ on every click: their *values* never
# identify anything, their presence only names the ad platform (platform tier). The campaign hierarchy ids of the
# platform (campaign > ad set / ad group > ad, plus the account-level id) are chosen by one advertiser account:
# operator tier, one subgroup (`inf:operator:ad_campaign`), because a shared ad id implies a shared campaign id.
AD_CLICK_PLATFORM: dict[str, str] = {
    "gclid": "google_ads", "gbraid": "google_ads", "wbraid": "google_ads", "dclid": "google_ads",
    "gad_source": "google_ads", "gad_campaignid": "google_ads",
    "fbclid": "meta_ads", "ttclid": "tiktok_ads", "msclkid": "microsoft_ads", "twclid": "x_ads",
    "li_fat_id": "linkedin_ads", "epik": "pinterest_ads", "sccid": "snapchat_ads", "yclid": "yandex_ads",
}
# utm_source values that name an ad platform (the value alone is a platform default, never an id)
AD_SOURCE_PLATFORM: dict[str, str] = {
    "google": "google_ads", "adwords": "google_ads", "googleads": "google_ads", "google_ads": "google_ads",
    "facebook": "meta_ads", "fb": "meta_ads", "ig": "meta_ads", "instagram": "meta_ads", "meta": "meta_ads",
    "tiktok": "tiktok_ads", "bing": "microsoft_ads", "microsoft": "microsoft_ads",
}
AD_PLATFORM_PL: dict[str, str] = {
    "google_ads": "Google Ads", "meta_ads": "Meta (Facebook/Instagram)", "tiktok_ads": "TikTok Ads",
    "microsoft_ads": "Microsoft Ads", "x_ads": "X Ads", "linkedin_ads": "LinkedIn Ads",
    "pinterest_ads": "Pinterest Ads", "snapchat_ads": "Snapchat Ads", "yandex_ads": "Yandex Direct",
    "ads": "platformy reklamowej",
}
# parameter -> level of the campaign hierarchy. `aatid` (10 digits on observed Shopline ad links, the length of a
# Google Ads customer id) is treated as an account-level id: assumed, not documented by the platform.
AD_ID_PARAMS: dict[str, str] = {
    "gad_campaignid": "campaign", "campaign_id": "campaign", "campaignid": "campaign", "utm_id": "campaign",
    "utm_campaign": "campaign", "adset_id": "ad_group", "adgroup_id": "ad_group", "adgroupid": "ad_group",
    "ad_id": "ad", "creative_id": "ad", "aatid": "account", "utm_content": "utm_id", "utm_term": "utm_id",
}
# generic names (also used by TDS / affiliate trackers): an ad id only next to an ad-platform marker
AD_ID_NEED_PLATFORM = {"campaign_id", "campaignid", "utm_id", "utm_campaign", "utm_content", "utm_term"}
AD_ID_VALUE = re.compile(r"\d{6,20}")
AD_LEVEL_PL: dict[str, str] = {
    "campaign": "identyfikator kampanii", "ad_group": "identyfikator grupy reklam", "ad": "identyfikator reklamy",
    "account": "identyfikator konta reklamowego (założenie)", "utm_id": "identyfikator kampanii w parametrze UTM",
}
# Polish waterfall labels of the SCAMLINKS kinds (KIND_PL lives in hydra/synapse.py: CR-SCAMLINKS-1 asks to merge).
KIND_PL_SCAMLINKS: dict[str, str] = {
    "ad_campaign_id": "Ten sam identyfikator kampanii reklamowej (Google Ads / Meta / TikTok)",
    "ad_platform": "Ta sama platforma reklamowa (wspólna dla wielu reklamodawców, samo w sobie nie łączy)",
    "shop_platform": "Ten sam kreator sklepów internetowych (wspólny dla wielu sklepów, samo w sobie nie łączy)",
    "product_path_skeleton": "Ten sam szkielet adresu produktu w sklepie",
    "shop_product_id": "Ten sam identyfikator produktu sklepu (ten sam sklep pod inną domeną)",
}


def ad_label_pl(value: str) -> str:
    """'google_ads:123' -> 'Ten sam identyfikator kampanii Google Ads' (value-level label for CR-SCAMLINKS-1)."""
    plat = value.split(":", 1)[0]
    return f"Ten sam identyfikator kampanii {AD_PLATFORM_PL.get(plat, AD_PLATFORM_PL['ads'])}"


def ad_platforms(params: list[tuple[str, str]]) -> dict[str, str]:
    """Ad platforms named by the click ids / utm_source of one URL: {platform: marker parameter}."""
    out: dict[str, str] = {}
    for k, v in params:
        kl = k.lower()
        if kl in AD_CLICK_PLATFORM and v:
            out.setdefault(AD_CLICK_PLATFORM[kl], kl)
        elif kl == "utm_source" and v.lower() in AD_SOURCE_PLATFORM:
            out.setdefault(AD_SOURCE_PLATFORM[v.lower()], f"utm_source={v.lower()}")
    return out


def ad_campaign_ids(params: list[tuple[str, str]]) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """(platforms, [(value, param, level)]) of one URL. value = '<platform>:<digits>': the platform prefix keeps
    ids of different platforms apart; the parameter name is not part of it, so the same id carried as
    `gad_campaignid` on one link and `utm_content` on another still matches. Unexpanded macros never match."""
    plats = ad_platforms(params)
    plat = next(iter(plats)) if len(plats) == 1 else "ads"
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for k, v in params:
        kl = k.lower()
        if kl not in AD_ID_PARAMS or not AD_ID_VALUE.fullmatch(v or ""):
            continue
        if kl in AD_ID_NEED_PLATFORM and not plats:
            continue
        val = f"{plat}:{v}"
        if val not in seen:
            seen.add(val)
            out.append((val, kl, AD_ID_PARAMS[kl]))
    return plats, out


def h16(s: str | bytes) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace") if isinstance(s, str) else s).hexdigest()[:16]


_NJ_URL = re.compile(r"https?:\\?/\\?/[^\s'\"`<>()\\]+", re.I)
_NJ_IP4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.])")
_NJ_IP6 = re.compile(r"(?<![\w:])(?=[0-9a-f:]*\d)[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:/\d{1,3})?(?![\w:])", re.I)
_NJ_PHONE = re.compile(r"(?<![\w+])\+?\d[\d\s().-]{7,16}\d(?![\w])")


def _mask_ip(m: re.Match, mark: str) -> str:
    import ipaddress

    try:
        ipaddress.ip_network(m.group(0), strict=False)
        return mark
    except ValueError:
        return m.group(0)


def norm_js(s: str) -> str:
    """Normalise code so rebuilt bundles with new ids/timestamps hash the same.

    Masks UUIDs, long numbers and (G06) per-deployment values a kit copy changes: URLs, e-mails, IPv4/IPv6
    addresses and CIDR ranges, phone numbers."""
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)^\s*//.*$", "", s)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "U", s, flags=re.I)
    s = _NJ_URL.sub("L", s)
    s = EMAIL_RX.sub("E", s)
    s = _NJ_IP6.sub(lambda m: _mask_ip(m, "I6"), s)
    s = _NJ_IP4.sub(lambda m: _mask_ip(m, "I4"), s)
    s = _NJ_PHONE.sub(lambda m: "P" if sum(c.isdigit() for c in m.group(0)) >= 9 else m.group(0), s)
    s = re.sub(r"\b\d{6,}\b", "N", s)
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------------------------- murmurhash3 (Shodan favicon)


def mmh3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3 x86_32, signed like `mmh3.hash` (pure Python; mmh3 is not in uv.lock)."""
    c1, c2, mask = 0xCC9E2D51, 0x1B873593, 0xFFFFFFFF
    h = seed & mask
    n = len(data)
    nblocks = n // 4
    for i in range(nblocks):
        k = int.from_bytes(data[4 * i : 4 * i + 4], "little")
        k = (k * c1) & mask
        k = ((k << 15) | (k >> 17)) & mask
        k = (k * c2) & mask
        h ^= k
        h = ((h << 13) | (h >> 19)) & mask
        h = (h * 5 + 0xE6546B64) & mask
    tail = data[4 * nblocks :]
    k = 0
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if tail:
        k ^= tail[0]
        k = (k * c1) & mask
        k = ((k << 15) | (k >> 17)) & mask
        k = (k * c2) & mask
        h ^= k
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & mask
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & mask
    h ^= h >> 16
    return h - (1 << 32) if h & 0x80000000 else h


def favicon_hash(b: bytes) -> int:
    """Shodan-compatible favicon hash: mmh3 of the base64 with newlines every 76 chars (`http.favicon.hash:`)."""
    return mmh3_32(base64.encodebytes(b))


# ------------------------------------------------------------------------------------------- data classes


@dataclass
class Body:
    url: str
    mime: str
    data: bytes
    entry: int | None  # HAR entry index


@dataclass
class HostInfo:
    n: int = 0
    ips: set[str] = field(default_factory=set)
    server: set[str] = field(default_factory=set)
    powered_by: set[str] = field(default_factory=set)
    cookies: set[str] = field(default_factory=set)
    cdn: set[str] = field(default_factory=set)
    first_entry: int | None = None


@dataclass
class HarParsed:
    artifact_id: str
    creator: str | None
    entry_url: str | None
    entry_index: int | None
    local_replay: bool
    page_titles: list[str]
    hosts: dict[str, HostInfo]
    redirects: list[dict[str, Any]]  # {kind, from, to, entry}
    posts: list[dict[str, Any]]  # {url, host, path_skel, fields, entry}
    requests: list[dict[str, Any]]  # {url, host, method, status, type, mime, entry}
    bodies: list[Body]
    error: str | None = None
    first_ts: str | None = None
    fetch: dict[str, Any] | None = None  # log._hydra_fetch (hydra.enrich.fetch output)
    tls: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)  # (entry, host, cert info)
    # 2xx HTML document responses: (entry, host, response headers in received order) for header-order genes
    doc_headers: list[tuple[int, str, list[tuple[str, str]]]] = field(default_factory=list)


@dataclass
class HtmlParsed:
    artifact_id: str
    src: str
    saved_from: str | None = None
    title: str | None = None
    lang: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    canonical: str | None = None
    icons: list[str] = field(default_factory=list)
    script_src: list[str] = field(default_factory=list)
    stylesheets: list[str] = field(default_factory=list)
    inline_scripts: list[str] = field(default_factory=list)  # raw code (redaction at gene time)
    forms: list[dict[str, Any]] = field(default_factory=list)
    tag_paths: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    comment_names: list[str] = field(default_factory=list)
    comment_texts: list[str] = field(default_factory=list)
    masthead_text: list[str] = field(default_factory=list)  # header/nav/footer/logo alt text
    comments: list[str] = field(default_factory=list)
    text: str = ""
    links: list[str] = field(default_factory=list)
    raw: str = ""
    error: str | None = None


# ------------------------------------------------------------------------------------------- HAR


def _decode_content(content: dict[str, Any]) -> bytes | None:
    t = content.get("text")
    if t is None:
        return None
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(t)
        except (ValueError, TypeError):
            return None
    return t.encode("utf-8", "surrogatepass") if isinstance(t, str) else None


def _hdr(headers: list[dict[str, Any]], name: str) -> list[str]:
    return [str(h.get("value", "")) for h in headers or [] if str(h.get("name", "")).lower() == name]


CDN_HEADERS = {
    "cf-ray": "cloudflare",
    "x-nf-request-id": "netlify",
    "x-vercel-id": "vercel",
    "x-amz-cf-id": "cloudfront",
    "cdn-pullzone": "bunny",
    "x-bunny-cache": "bunny",
    "x-github-request-id": "github",
    "x-goog-generation": "gcs",
    "x-served-by": "fastly",
    "x-litespeed-cache": "litespeed",
    "x-azure-ref": "azure",
}


@lru_cache(maxsize=4)  # parsed files hold response bodies: keep the cache small
def _parse_har_cached(path: str, size: int, mtime: float, artifact_id: str) -> HarParsed:
    return _parse_har(Path(path), artifact_id)


def parse_har(a: ResolvedArtifact) -> HarParsed:
    st = a.path.stat()
    if st.st_size > MAX_FILE_BYTES:
        return HarParsed(a.meta.artifact_id, None, None, None, False, [], {}, [], [], [], [], error=FILE_TOO_LARGE)
    return _parse_har_cached(str(a.path), st.st_size, st.st_mtime, a.meta.artifact_id)


def _parse_har(path: Path, artifact_id: str) -> HarParsed:
    try:
        har = json.loads(path.read_bytes().decode("utf-8", errors="replace"))
        log = har["log"]
    except (ValueError, KeyError, TypeError) as exc:
        return HarParsed(artifact_id, None, None, None, False, [], {}, [], [], [], [], error=f"{type(exc).__name__}")
    entries = log.get("entries") or []
    pages = log.get("pages") or []
    order = sorted(range(len(entries)), key=lambda i: str(entries[i].get("startedDateTime", "")))
    hosts: dict[str, HostInfo] = defaultdict(HostInfo)
    redirects: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []
    reqs: list[dict[str, Any]] = []
    bodies: list[Body] = []
    tls: list[tuple[int, str, dict[str, Any]]] = []
    doc_headers: list[tuple[int, str, list[tuple[str, str]]]] = []
    local_replay = False
    entry_url: str | None = None
    entry_index: int | None = None
    for p in pages:
        t = str(p.get("title", ""))
        if t.startswith(("http://", "https://")):
            entry_url = t
            break
        if t.startswith("file:"):
            local_replay = True
    for i in order:
        e = entries[i]
        rq, rs = e.get("request") or {}, e.get("response") or {}
        u = str(rq.get("url", ""))
        if u.startswith("file:"):
            local_replay = True
        sp = urlsplit(u)
        if sp.scheme not in ("http", "https"):
            continue
        host = (sp.hostname or "").lower()
        content = rs.get("content") or {}
        mime = str(content.get("mimeType", "")).split(";")[0].strip().lower()
        status = int(rs.get("status") or 0)
        rtype = e.get("_resourceType")
        if (
            entry_url is None
            and (rtype == "document" or (mime == "text/html" and status < 300))
            and not is_boilerplate(host)
        ):
            entry_url, entry_index = u, i
        hi = hosts[host]
        hi.n += 1
        if hi.first_entry is None:
            hi.first_entry = i
        ip = e.get("serverIPAddress")
        if ip:
            hi.ips.add(str(ip).strip("[]"))
        rh = rs.get("headers") or []
        for v in _hdr(rh, "server"):
            hi.server.add(v.strip()[:80])
        for v in _hdr(rh, "x-powered-by"):
            hi.powered_by.add(v.strip()[:80])
        for v in _hdr(rh, "set-cookie"):
            if "=" in v:
                hi.cookies.add(v.split("=", 1)[0].strip()[:60])
        for c in rs.get("cookies") or []:
            if c.get("name"):
                hi.cookies.add(str(c["name"])[:60])
        for hn in {str(h.get("name", "")).lower() for h in rh}:
            if hn in CDN_HEADERS:
                hi.cdn.add(CDN_HEADERS[hn])
        reqs.append(
            {
                "url": u,
                "host": host,
                "method": rq.get("method"),
                "status": status,
                "type": rtype,
                "mime": mime,
                "entry": i,
            }
        )
        loc = rs.get("redirectURL") or next(iter(_hdr(rh, "location")), "")
        if 300 <= status < 400 and loc:
            redirects.append({"kind": f"http{status}", "from": u, "to": loc, "entry": i})
        elif rs.get("_redirect_kind") == "meta_refresh" and rs.get("_redirect_to"):
            redirects.append({"kind": "meta_refresh", "from": u, "to": str(rs["_redirect_to"]), "entry": i})
        if isinstance(e.get("_tls"), dict):
            tls.append((i, host, e["_tls"]))
        if 200 <= status < 300 and (rtype == "document" or mime == "text/html") and rh:
            doc_headers.append(
                (i, host, [(str(h.get("name", "")), str(h.get("value", ""))[:2000]) for h in rh if h.get("name")])
            )
        if str(rq.get("method", "")).upper() in ("POST", "PUT", "PATCH"):
            pd = rq.get("postData") or {}
            names = [str(p.get("name")) for p in pd.get("params") or [] if p.get("name")]
            txt = pd.get("text") or ""
            if not names and txt:
                try:
                    j = json.loads(txt)
                    if isinstance(j, dict):
                        names = sorted(map(str, j.keys()))
                except ValueError:
                    names = [k for k, _ in parse_qsl(txt)][:60]
            if txt:
                bodies.append(Body(u + "#post", "text/plain", txt.encode("utf-8", "replace")[:MAX_TEXT_SCAN], i))
            posts.append(
                {
                    "url": u,
                    "host": host,
                    "path_skel": path_skeleton(sp.path),
                    "fields": names[:60],
                    "entry": i,
                    "mime": pd.get("mimeType"),
                }
            )
        b = _decode_content(content)
        if b:
            bodies.append(Body(u, mime, b, i))
    first_ts = str(entries[order[0]].get("startedDateTime") or "") or None if order else None
    return HarParsed(
        artifact_id,
        (log.get("creator") or {}).get("name"),
        entry_url,
        entry_index,
        local_replay,
        [str(p.get("title", ""))[:300] for p in pages],
        dict(hosts),
        redirects,
        posts,
        reqs,
        bodies,
        first_ts=first_ts,
        fetch=log.get("_hydra_fetch") if isinstance(log.get("_hydra_fetch"), dict) else None,
        tls=tls,
        doc_headers=doc_headers,
    )


# ------------------------------------------------------------------------------------------- HTML

SKEL_SKIP = {"script", "style", "noscript", "svg", "path", "br", "meta", "link", "#comment"}
COMMENT_BLOCK = re.compile(r"comment|review|fb-?com|testimonial|opinion|komentarz", re.I)
NAME_CLASS = re.compile(r"name|author|user|nick", re.I)
TEXT_CLASS = re.compile(r"text|body|content|message|msg|tresc", re.I)


@lru_cache(maxsize=4)  # parsed files hold response bodies: keep the cache small
def _parse_html_cached(path: str, size: int, mtime: float, artifact_id: str, src: str, mime: str) -> HtmlParsed:
    raw = Path(path).read_bytes()
    if mime == "multipart/related" or raw[:200].lower().find(b"mime-version") >= 0:
        html, loc = mhtml_root(raw)
        hp = parse_html_bytes(html or b"", artifact_id, src)
        hp.saved_from = hp.saved_from or loc
        return hp
    return parse_html_bytes(raw, artifact_id, src)


def parse_html(a: ResolvedArtifact) -> HtmlParsed:
    st = a.path.stat()
    if st.st_size > MAX_FILE_BYTES:
        return HtmlParsed(artifact_id=a.meta.artifact_id, src=a.meta.path_in_incident, error=FILE_TOO_LARGE)
    return _parse_html_cached(
        str(a.path), st.st_size, st.st_mtime, a.meta.artifact_id, a.meta.path_in_incident, a.meta.mime or ""
    )


def mhtml_root(raw: bytes) -> tuple[bytes | None, str | None]:
    msg = email.message_from_bytes(raw)
    loc = msg.get("Snapshot-Content-Location")
    for p in msg.walk():
        if not p.is_multipart() and p.get_content_type() == "text/html":
            payload = p.get_payload(decode=True)
            return (payload if isinstance(payload, bytes) else None), loc
    return None, loc


def mhtml_parts(raw: bytes) -> list[tuple[str | None, str, bytes]]:
    msg = email.message_from_bytes(raw)
    out = []
    for p in msg.walk():
        if p.is_multipart():
            continue
        payload = p.get_payload(decode=True)
        out.append((p.get("Content-Location"), p.get_content_type(), payload if isinstance(payload, bytes) else b""))
    return out


def parse_html_bytes(raw: bytes, artifact_id: str, src: str) -> HtmlParsed:
    from bs4 import BeautifulSoup, Comment

    txt = raw.decode("utf-8", errors="replace")
    d = HtmlParsed(artifact_id=artifact_id, src=src, raw=txt[:MAX_TEXT_SCAN])
    m = re.search(r"<!--\s*saved from url=\(\d+\)(\S+)\s*-->", txt[:4000])
    if m and m.group(1).startswith(("http://", "https://")):
        d.saved_from = m.group(1)
    try:
        soup = BeautifulSoup(txt, "lxml")
    except Exception as exc:  # noqa: BLE001 — parser failure becomes a failed gene upstream
        d.error = f"{type(exc).__name__}"
        return d
    # Browser artefacts that are not part of the page (Google Translate widget, blank iframes).
    for t in soup.select("#goog-gt-tt, .goog-te-banner-frame, .skiptranslate, #google_translate_element"):
        t.decompose()
    d.title = soup.title.get_text(" ", strip=True)[:300] if soup.title else None
    html = soup.find("html")
    d.lang = (str(html.get("lang") or "").lower() or None) if html else None
    for mt in soup.find_all("meta"):
        k = mt.get("property") or mt.get("name") or mt.get("http-equiv")
        if k and mt.get("content") is not None:
            d.meta[str(k).lower()] = str(mt.get("content"))[:300]
    can = soup.find("link", rel=lambda r: bool(r) and "canonical" in r)
    d.canonical = str(can.get("href")) if can and can.get("href") else None
    d.icons = [
        str(x.get("href"))
        for x in soup.find_all("link", rel=lambda r: bool(r) and any("icon" in y for y in r))
        if x.get("href")
    ]
    d.stylesheets = [
        str(x.get("href")) for x in soup.find_all("link", rel=lambda r: bool(r) and "stylesheet" in r) if x.get("href")
    ]
    for s in soup.find_all("script"):
        if s.get("src"):
            d.script_src.append(str(s.get("src"))[:400])
        else:
            code = s.string or s.get_text() or ""
            if len(code.strip()) > 40:
                d.inline_scripts.append(code[:MAX_TEXT_SCAN])
    for f in soup.find_all("form"):
        fields = sorted(
            {
                str(i.get("name") or i.get("id") or "")
                for i in f.find_all(["input", "select", "textarea"])
                if (i.get("name") or i.get("id"))
            }
        )
        hidden = sorted({str(i.get("name")) for i in f.find_all("input", type="hidden") if i.get("name")})
        d.forms.append(
            {
                "action": f.get("action"),
                "method": str(f.get("method") or "get").lower(),
                "fields": fields,
                "hidden": hidden,
            }
        )

    # DOM skeleton: root-to-node tag paths in document order (depth-capped)
    def walk(node, stack, depth):
        for ch in getattr(node, "children", []):
            name = getattr(ch, "name", None)
            if name is None or name in SKEL_SKIP:
                continue
            st = stack + [name]
            d.tag_paths.append("/".join(st[-6:]))
            if depth < 60:
                walk(ch, st, depth + 1)

    body = soup.body or soup
    walk(body, [], 0)
    d.ids = [str(t.get("id")) for t in soup.find_all(id=True)]
    d.classes = [str(c) for t in soup.find_all(class_=True) for c in (t.get("class") or [])]
    # fake comment blocks
    for blk in soup.find_all(class_=COMMENT_BLOCK):
        nm = blk.find(class_=NAME_CLASS)
        if nm:
            t = nm.get_text(" ", strip=True)
            if 2 < len(t) < 50:
                d.comment_names.append(t)
        tx = blk.find(class_=TEXT_CLASS)
        if tx:
            t = tx.get_text(" ", strip=True)
            if 15 < len(t) < 2000:
                d.comment_texts.append(t)
    # masthead: header / nav / footer text + logo alts
    for sel in ("header", "nav", "footer", "[class*=logo]", "[id*=logo]", "[class*=masthead]", "[class*=brand]"):
        for t in soup.select(sel)[:10]:
            d.masthead_text.append(t.get_text(" ", strip=True)[:500])
            for im in t.find_all("img"):
                d.masthead_text.append(" ".join(str(im.get(k) or "") for k in ("alt", "src", "title"))[:300])
    for im in soup.find_all("img"):
        alt = str(im.get("alt") or "")
        src = str(im.get("src") or "")
        if "logo" in (alt + src).lower():
            d.masthead_text.append(f"{alt} {src}"[:300])
    for sc in soup(["script", "style", "noscript"]):
        sc.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        t = str(c).strip()
        if len(t) > 3 and not t.startswith("saved from"):
            d.comments.append(t[:300])
        c.extract()
    d.text = soup.get_text(" ", strip=True)[:500_000]
    d.links = [str(a.get("href")) for a in soup.find_all("a", href=True)][:2000]
    return d


# ------------------------------------------------------------------------------------------- text scans


@dataclass
class Scan:
    trackers: dict[str, str] = field(default_factory=dict)  # "fb_pixel:123" -> src
    opids: dict[str, str] = field(default_factory=dict)
    tds: dict[str, str] = field(default_factory=dict)
    cloak: dict[str, str] = field(default_factory=dict)
    js_redirects: dict[tuple[str, str], str] = field(default_factory=dict)
    endpoints: dict[str, str] = field(default_factory=dict)
    libs: dict[str, str] = field(default_factory=dict)
    linked_hosts: dict[str, str] = field(default_factory=dict)
    cloak_impl: dict[str, str] = field(default_factory=dict)  # masked cloaking snippet hash -> src


def scan_text(txt: str, src: str, acc: Scan, *, redirects: bool = True) -> None:
    txt = txt[:MAX_TEXT_SCAN]
    for name, rxs in TRACKER_RX.items():
        for rx in rxs:
            for m in rx.findall(txt):
                acc.trackers.setdefault(f"{name}:{m}", src)
    for name, rx in OPID_RX.items():
        for m in rx.findall(txt):
            acc.opids.setdefault(f"{name}:{str(m).lower()}", src)
    for name, rx in TDS_SIGS:
        if rx.search(txt):
            acc.tds.setdefault(name, src)
    fired = [name for name, rx in CLOAK_RX.items() if rx.search(txt)]
    for name in fired:
        acc.cloak.setdefault(name, src)
    if fired and redirects:  # first-party / inline code only: a shared library would link unrelated sites
        blocks = _SCRIPT_BLOCK.findall(txt) if "<script" in txt[:200_000].lower() else [txt]
        for code in blocks:
            hv = cloak_impl_hash(code[:500_000])
            if hv:
                acc.cloak_impl.setdefault(hv, src)
    if redirects:
        for kind, rx in REDIR_RX:
            for m in rx.findall(txt):
                if m.startswith(("#", "javascript", "data:")) or "+" in m:
                    continue
                acc.js_redirects.setdefault((kind, m[:300]), src)
        for m in META_REFRESH.findall(txt):
            acc.js_redirects.setdefault(("meta_refresh", m[:300]), src)
    for m in ENDPOINT_RX.findall(txt):
        path = urlsplit(m).path if m.startswith("http") else m
        if STATIC_EXT.search(path) or "/wp-" in m or "schema.org" in m or "w3.org" in m or len(path) < 4:
            continue
        acc.endpoints.setdefault(m[:200], src)
    for m in LIB_RX.findall(txt):
        acc.libs.setdefault(f"{m[0].lower().replace('.js', '')}@{m[1]}", src)
    for m in URL_HOST_RX.findall(txt):
        h = m.lower().strip(".")
        if valid_host(h):
            acc.linked_hosts.setdefault(h, src)


def is_text_mime(mime: str) -> bool:
    return mime.startswith(
        ("text/", "application/javascript", "application/x-javascript", "application/json", "application/ld+json")
    )


# ------------------------------------------------------------------------------------------- incident bundle


@dataclass
class WebBundle:
    """Everything parsed from one incident's web material."""

    hars: list[HarParsed]
    htmls: list[HtmlParsed]
    assets: list[tuple[ResolvedArtifact, bytes]]  # saved _files resources
    landing_urls: list[tuple[str, str, str | None]]  # (url, source, artifact_id)


def _is_har(a: ResolvedArtifact) -> bool:
    return a.meta.role == "har" or a.meta.path_in_incident.lower().endswith(".har")


def _is_html(a: ResolvedArtifact) -> bool:
    p = a.meta.path_in_incident.lower()
    return a.meta.role == "landing_html" or (a.meta.role != "landing_asset" and p.endswith((".html", ".htm", ".mhtml")))


def _is_asset(a: ResolvedArtifact) -> bool:
    return a.meta.role == "landing_asset"


def bundle(inc: ResolvedIncident) -> WebBundle:
    key = (inc.incident.id, tuple(a.meta.artifact_id for a in inc.artifacts))
    hit = _BUNDLES.get(key)
    if hit is not None:
        return hit
    hars = [parse_har(a) for a in inc.artifacts if _is_har(a)]
    htmls = [parse_html(a) for a in inc.artifacts if _is_html(a) and not _is_har(a)]
    assets: list[tuple[ResolvedArtifact, bytes]] = []
    for a in inc.artifacts:
        if _is_asset(a):
            try:
                if a.meta.size <= 20_000_000:
                    assets.append((a, a.path.read_bytes()))
            except OSError:
                continue
    urls: list[tuple[str, str, str | None]] = []
    for h in hars:
        if h.entry_url and not h.local_replay:
            urls.append((h.entry_url, "har_entry", h.artifact_id))
    for d in htmls:
        if d.saved_from:
            urls.append((d.saved_from, "saved_from", d.artifact_id))
    for a in inc.artifacts:
        if a.meta.role == "url":
            try:
                for line in a.path.read_text(errors="replace").splitlines()[:50]:
                    line = line.strip()
                    if line.startswith(("http://", "https://")):
                        urls.append((line, "url_input", a.meta.artifact_id))
            except OSError:
                pass
        elif a.meta.path_in_incident.endswith("manifest.json") and a.meta.size < 5_000_000:
            try:
                j = json.loads(a.path.read_text(errors="replace"))
                if isinstance(j, dict) and str(j.get("page_url", "")).startswith("http"):
                    urls.append((j["page_url"], "capture_manifest", a.meta.artifact_id))
            except (OSError, ValueError):
                pass
    for k in ("url", "landing_url", "entry_url"):
        v = inc.incident.meta.get(k)
        if isinstance(v, str) and v.startswith("http"):
            urls.append((v, "incident_meta", None))
    out = WebBundle(hars, htmls, assets, [(redact_url(u), s, aid) for u, s, aid in urls])
    # one or two incidents at a time: bundles hold asset bytes, so never keep more
    while len(_BUNDLES) >= BUNDLE_CACHE:
        _BUNDLES.pop(next(iter(_BUNDLES)))
    _BUNDLES[key] = out
    return out


BUNDLE_CACHE = 2
_BUNDLES: dict[tuple, WebBundle] = {}


def first_party_hosts(b: WebBundle) -> set[str]:
    """Operator-side hosts: landing URL hosts + non-boilerplate hosts contacted in the HARs."""
    out = set()
    for u, _, _ in b.landing_urls:
        h = (urlsplit(u).hostname or "").lower()
        if valid_host(h) and not is_boilerplate(h):
            out.add(h)
    for h in b.hars:
        if h.local_replay:
            continue
        for host in h.hosts:
            if valid_host(host) and not is_boilerplate(host):
                out.add(host)
    return out


OPERATOR_HOST_KINDS = ("postback_host", "form_event_backend", "crm_host")


def operator_hosts(b: WebBundle) -> set[str]:
    """Backend hosts named in code/bodies (postback, form-event backend, CRM): operator infrastructure to enrich."""
    cached = getattr(b, "_operator_hosts", None)
    if cached is not None:
        return cached
    out: set[str] = set()
    texts = [d.raw for d in b.htmls] + [c for d in b.htmls for c in d.inline_scripts]
    texts += [
        body.data.decode("utf-8", "replace")
        for h in b.hars
        for body in h.bodies
        if is_text_mime(body.mime) or body.url.endswith("#post")
    ]
    texts += [
        data.decode("utf-8", "replace")
        for a, data in b.assets
        if a.meta.path_in_incident.lower().split(".pobrane")[0].endswith((".js", ".html", ".htm"))
    ]
    for t in texts:
        for k in OPERATOR_HOST_KINDS:
            for m in OPID_RX[k].findall(t[:MAX_TEXT_SCAN]):
                h = str(m).lower().strip(".")
                if valid_host(h) and not is_boilerplate(h):
                    out.add(h)
    b._operator_hosts = out  # type: ignore[attr-defined]
    return out


def counter_top(values: list[str], n: int) -> list[str]:
    return [v for v, _ in Counter(values).most_common(n)]


# ------------------------------------------------------------------------------------------- tiers
# dependency_group = "inf:<tier>:<subgroup>". Tiers (architect, 2026-11-21): operator = identifies one
# buyer/team/account; platform = a toolchain shared by many customers (TDS, lead SDK, cloaker); kit = copied
# template/assets/brand; boilerplate = CDN, libraries, generic headers (0 bits); label = a property used to label a
# cluster, never a link (CERT Polska list). CORE caps platform+kit at about 4 bits in total.
# Correlated genes share one subgroup, so they share one bit budget.
TIERS: dict[str, tuple[str, str]] = {
    # landing identity (one subgroup: host, domain, URL and its skeletons are one observation)
    "entry_url": ("operator", "landing"),
    "landing_host": ("operator", "landing"),
    "registrable_domain": ("operator", "landing"),
    "url_label_path_skeleton": ("kit", "url_shape"),
    "url_label_skeleton": ("kit", "url_shape"),
    "url_digit_skeleton": ("operator", "url_skeleton"),
    "url_path_skeleton": ("platform", "url_path"),
    "har_local_replay": ("boilerplate", "har_meta"),
    "har_creator": ("boilerplate", "har_meta"),
    # URL parameters / TDS
    "utm_pattern": ("platform", "url_params"),
    "url_param_set": ("platform", "url_params"),
    "tds_param": ("boilerplate", "tds_param"),
    "tds_value": ("operator", "tds_value"),
    "ad_campaign_id": ("operator", "ad_campaign"),  # SCAMLINKS: campaign/ad group/ad/account ids of an ad platform
    "ad_platform": ("platform", "ad_platform"),  # Google Ads / Meta / TikTok: shared by every advertiser
    "shop_platform": ("platform", "shop_builder"),  # Shopline / Shopify / ...: shared by every shop (genome/adshop.py)
    "product_path_skeleton": ("kit", "shop_product_path"),
    "shop_product_id": ("operator", "shop_store"),
    "unexpanded_macro": ("platform", "url_params"),
    "tds_signature": ("platform", "tds"),
    "capi_token_hash": ("operator", "tracker"),
    "tracker_id": ("operator", "tracker"),
    # network graph
    "host_contacted": ("operator", "hosts"),
    "domain_contacted": ("operator", "hosts"),
    "linked_host": ("operator", "linked_hosts"),
    "thirdparty_host": ("boilerplate", "thirdparty"),
    "redirect_hop": ("operator", "redirect"),
    "redirect_chain": ("operator", "redirect"),
    "redirect_skeleton": ("platform", "redirect_shape"),
    "js_redirect": ("platform", "redirect_shape"),
    "server_ip": ("operator", "ip"),
    "server_ip24": ("operator", "ip"),
    "dns_ip": ("operator", "ip"),
    "dns_ip24": ("operator", "ip"),
    "http_server": ("boilerplate", "headers"),
    "x_powered_by": ("platform", "headers"),
    "cookie_name": ("platform", "headers"),
    "cdn_provider": ("boilerplate", "cdn"),
    "lead_endpoint": ("operator", "lead_endpoint"),
    "tracking_endpoint": ("platform", "endpoints"),
    "post_fields": ("platform", "endpoints"),
    "api_path": ("platform", "endpoints"),
    "code_endpoint": ("platform", "endpoints"),
    "cloak_feature": ("platform", "cloak"),
    "cloak_impl": ("kit", "cloak_impl"),
    "white_page_signal": ("boilerplate", "fetch"),
    "white_page_suspicion": ("boilerplate", "fetch"),
    "script_lib": ("boilerplate", "libs"),
    "asset_sha256_lib": ("boilerplate", "libs"),
    # DOM / kit
    "page_title": ("kit", "page_meta"),
    "og_meta": ("kit", "page_meta"),
    "canonical_host": ("kit", "page_meta"),
    "html_lang": ("boilerplate", "page_meta"),
    "generator": ("platform", "page_meta"),
    "dom_shingle": ("kit", "dom_skeleton"),
    "dom_skeleton_hash": ("kit", "dom_skeleton"),
    "dom_token": ("kit", "dom_template"),
    "html_comment": ("kit", "dom_template"),
    "inline_script_hash": ("kit", "scripts"),
    "script_name": ("kit", "scripts"),
    "js_build_hash": ("kit", "js_build"),
    "css_build_hash": ("kit", "css_build"),
    "tlsh_html_skeleton": ("kit", "fuzzy_build"),
    "tlsh_js": ("kit", "fuzzy_build"),
    "tlsh_css": ("kit", "fuzzy_build"),
    "asset_sha256": ("kit", "assets"),
    "favicon_mmh3": ("kit", "favicon"),
    "form_fields": ("kit", "lead_form"),
    "lead_form_kit": ("kit", "lead_form"),
    "form_action": ("platform", "lead_form_action"),
    "comment_names_set": ("kit", "comments"),
    "fake_comment_block": ("kit", "comments"),
    "fake_comment": ("kit", "comments"),
    "media_brand": ("kit", "brand"),
    "entity_phone": ("operator", "contact"),
    "entity_email": ("operator", "contact"),
    "entity_iban": ("operator", "contact"),
    "entity_email_domain": ("operator", "contact"),
    # enrichment
    "registrar": ("platform", "registration"),
    "registration_day": ("operator", "registration"),
    "registrar_day": ("operator", "registration"),
    "registrar_window": ("operator", "registration"),  # G13; large retail registrars: boilerplate (genome/lure.py)
    "brand_offdomain": ("boilerplate", "brand_offdomain"),  # G13 explanation fact (genome/lure.py)
    "kit_fileset_band": ("kit", "kit_fileset"),  # G06 (genome/kitset.py)
    "kit_file": ("boilerplate", "kit_fileset_member"),  # G06 set members, 0 bits
    "registration_month": ("boilerplate", "registration_month"),
    "domain_age_at_observation": ("boilerplate", "age"),
    "cert_first_month": ("boilerplate", "registration_month"),
    "ns_host": ("operator", "ns"),
    "ns_set": ("operator", "ns"),
    "mx_host": ("operator", "mx"),
    "dns_cname": ("operator", "cname"),
    "dns_verification_token": ("operator", "dns_token"),
    "dns_rcode": ("boilerplate", "dns_state"),
    "dns_wildcard": ("platform", "dns_wildcard"),
    "dns_zone_ip": ("operator", "ip"),
    "dns_zone_cname": ("operator", "cname"),
    "inf_parked": ("label", "parked"),
    "http_header_order_hash": ("platform", "header_order"),
    "http_security_headers": ("platform", "security_headers"),
    "http_csp_hash": ("kit", "csp"),
    "tls_probe_hash": ("platform", "tls_probe"),
    "asn": ("platform", "asn"),
    "as_org": ("platform", "asn"),
    "bgp_prefix": ("platform", "asn"),
    "cert_issuer": ("boilerplate", "cert_issuer"),
    "tls_issuer": ("boilerplate", "cert_issuer"),
    "tls_invalid": ("boilerplate", "cert_issuer"),
    "tls_cert_sha256": ("operator", "cert"),
    "cert_san_sibling": ("operator", "cert"),
    "cert_san_domain": ("operator", "cert"),
    "cert_pl_listed_host": ("label", "cert_pl"),
    "cert_pl_listed_parent": ("label", "cert_pl"),
}
# operator_id sub-kinds: which are toolchain-level (platform) rather than one account
OPID_PLATFORM = {"form_event_backend", "fb_page_ref"}


def dependency_group(kind: str, tier: str | None = None, sub: str | None = None) -> str:
    t, s = TIERS.get(kind, ("platform", kind))
    return f"inf:{tier or t}:{sub or s}"


_SALT = None


def pii_hash(value: str) -> str:
    """Salted sha256 prefix for personal data / secrets (salt: HYDRA_HASH_SALT, else a project constant).

    Stable within a deployment so equal values still link; the raw value is never stored."""
    global _SALT
    if _SALT is None:
        import os

        _SALT = os.environ.get("HYDRA_HASH_SALT", "hydra-nexus-nask-2026")
    return "h:" + hashlib.sha256(f"{_SALT}|{value}".encode("utf-8", "replace")).hexdigest()[:16]


IP_VALUE_KINDS = {"server_ip", "dns_ip", "server_ip24", "dns_ip24", "bgp_prefix", "dns_zone_ip"}


def _non_public(value: str) -> bool:
    """Private, ULA, link-local, loopback, reserved, CGNAT and other bogon addresses/networks."""
    import ipaddress

    try:
        if "/" in value:
            return not ipaddress.ip_network(value, strict=False).is_global
        return not ipaddress.ip_address(value.strip("[]")).is_global
    except ValueError:
        return False


def _apply_warninglist(kind: str, value: str, group: str, quality: float, note: str | None):
    """Tier overrides that only ever lower a tier: non-public IPs (never operator evidence: e.g. Cloudflare
    Tunnel's ULA answer fd10:aec2:5dae:: shared by 73 hosts on the public corpus) and MISP warninglists.

    Order, highest to lowest: operator > platform / kit > label / boilerplate. `label` (sinkhole IP, real bank or
    brand site, DNS block page, phishing simulator) becomes `inf:label:<sub>`: a cluster attribute, never link
    evidence (synapse.level -> none)."""
    from hydra.enrich import warninglists

    if kind in IP_VALUE_KINDS and _non_public(value):
        tag = "[non-public address]"
        return "inf:boilerplate:private_ip", min(quality, 0.05), f"{note} {tag}" if note else tag
    hit = warninglists.classify(kind, value)
    if hit is None:
        return group, quality, note
    parts = group.split(":")
    tier = parts[1] if len(parts) >= 3 else "platform"
    effect = hit.effect
    if effect == "parked_domain" and kind not in ("ns_host", "ns_set", "dns_ip", "dns_ip24", "dns_zone_ip"):
        effect = "platform"  # e.g. Namecheap forwarding MX is on the parking-NS list: shared service, not parking
    tag = f"warninglist:{hit.list_name}" + (f" {effect}" if effect in ("label", "parked_domain") else "")
    note = f"{note} [{tag}]" if note else f"[{tag}]"
    if tier in ("boilerplate", "label"):
        return group, quality, note  # already zero-weight: never re-tiered
    if effect == "boilerplate":
        return f"inf:boilerplate:{parts[-1]}", min(quality, 0.1), note
    if effect == "label":
        sub = warninglists.LABEL_SUB.get(hit.list_name, hit.list_name.replace("-", "_"))
        return f"inf:label:{sub}", min(quality, 0.1), note
    if effect == "parked_domain":
        return "inf:label:parked_domain", min(quality, 0.1), note
    if effect == "platform" and tier == "operator":
        return f"inf:platform:{parts[-1]}", min(quality, 0.5), note
    return group, quality, note


class GeneSink:
    """Collects genes of one extractor, de-duplicated on (kind, value); first evidence wins.

    `group` argument: None = tier table default; otherwise a tier override ("boilerplate", ...) or a full
    "inf:<tier>:<sub>" string."""

    def __init__(self, incident_id: str, extractor: str):
        self.incident_id = incident_id
        self.extractor = extractor
        self.genes: list[Gene] = []
        self._seen: set[tuple[str, str | None]] = set()

    def add(
        self,
        kind: str,
        value: str | None,
        group: str | None,
        *,
        artifact_id: str | None = None,
        har_entry: int | None = None,
        dom_path: str | None = None,
        note: str | None = None,
        quality: float = 1.0,
        status: GeneStatus = GeneStatus.OBSERVED,
        provenance: Provenance = Provenance.OBS,
        enrichment_key: str | None = None,
    ) -> None:
        if value is not None:
            value = redact_text(str(value))[:500]
        if group is None or group not in ("operator", "platform", "kit", "boilerplate", "label"):
            group = group if (group or "").startswith("inf:") else dependency_group(kind)
        else:
            group = dependency_group(kind, tier=group)
        k = (kind, value)
        if k in self._seen:
            return
        self._seen.add(k)
        if status == GeneStatus.OBSERVED and value is not None:
            group, quality, note = _apply_warninglist(kind, value, group, quality, note)
        self.genes.append(
            Gene(
                incident_id=self.incident_id,
                family=Family.INF,
                kind=kind,
                value=value,
                quality=quality,
                status=status,
                provenance=provenance,
                extractor=self.extractor,
                dependency_group=group,
                evidence=EvidencePointer(
                    artifact_id=artifact_id,
                    har_entry=har_entry,
                    dom_path=dom_path,
                    enrichment_key=enrichment_key,
                    note=(redact_text(note)[:300] if note else None),
                ),
            )
        )

    def missing(self, kind: str, status: GeneStatus, note: str, group: str | None = None) -> None:
        self.add(kind, None, group, status=status, note=note)

    def result(self) -> ExtractResult:
        return ExtractResult(genes=self.genes)
