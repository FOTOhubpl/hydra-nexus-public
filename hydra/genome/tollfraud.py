"""Toll-fraud chain genes: app-store ids, Meta Ad Library ids, premium-rate SMS, carrier billing, cloud load
balancers and shared TLS certificates.

Motivation and link chain: CERT Polska, "Fałszywe reklamy i złośliwe aplikacje — analiza operacji toll fraud"
(2026-09-23, https://cert.pl/posts/2026/09/analiza-tollfraud/). CERT linked Meta ads to Google Play apps through
exact ad destination URLs (the Play package), identical creative file SHA-256, Ad Library ids and advertiser
profile ids, C2 hosts behind Aliyun load balancers, one TLS certificate SHA-256 reused on several endpoints, and
premium SMS short codes (92505 / 92512 / 92513) checked in the UKE register. Similar names or similar ad themes
were explicitly not enough for CERT; this module therefore only emits exact identifiers. Written from the post's
description and public format documentation (Play / App Store URL formats, Android intent URI syntax, RFC 5724
`sms:` URIs, cloud provider hostname formats); no third-party code.

Pure text finders (`find_*`) return `txt_entities.Entity` objects with offsets into the original text; the TXT
extractor uses them through `operator_ids.find_operator_ids`, the INF extractor `inf_tollfraud` runs them over the
saved web material. TXT and INF emit identical values for the same thing so they match across families.

Kinds, tiers and value forms (docs/genes/INF.md section 11):
- app_package_id        operator   "android:<package>" | "ios:id<digits>"   (well-known app -> boilerplate,
                                                                              impersonated brand app -> kit)
- app_developer_id      operator   "play_dev:<digits>" | "play_dev_name:<name>" | "ios_dev:<digits>"
- platform_ad_id        operator   "<digits>" (same value form and group as META info.json, hydra/genome/meta.py)
- platform_page_id      operator   "<digits>" (Ad Library view_all_page_id / page_id = advertiser page id)
- premium_sms           operator   "<short code>:<KEYWORD>" (pure-digit keyword tokens masked as "#")
- premium_sms_code      platform   "<short code>" (one code serves many billing customers)
- carrier_billing_marker kit       marker name (DCB / premium-billing wording of the page)
- cloud_lb_host         platform   "<provider>:<region or ->" (a cloud load balancer service)
- cloud_lb_name         operator   "<provider>:<instance id>" (per-customer LB / distribution instance)
- tls_cert_sha256       operator   lowercase hex SHA-256 of the DER certificate (har.py; text finder here);
                                   shared CDN / multi-tenant certificates -> boilerplate (`shared_cert_reason`)
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.enrich.domains import is_boilerplate, valid_host
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.txt_entities import Entity, find_hosts, orig_span, refang
from hydra.genome.webparse import MAX_TEXT_SCAN, URL_HOST_RX, GeneSink, bundle, is_text_mime

EXTRACTOR = "inf_tollfraud@1"

# kind -> (INF dependency group, starting quality)
TOLL_KINDS: dict[str, tuple[str, float]] = {
    "app_package_id": ("inf:operator:app_package", 1.0),
    "app_developer_id": ("inf:operator:app_developer", 0.9),
    "premium_sms": ("inf:operator:premium_sms", 0.9),
    "premium_sms_code": ("inf:platform:premium_sms_code", 0.6),
    "carrier_billing_marker": ("inf:kit:carrier_billing", 0.5),
    "cloud_lb_host": ("inf:platform:cloud_lb", 0.4),
    "cloud_lb_name": ("inf:operator:cloud_lb_name", 0.9),
    "tls_cert_sha256": ("inf:operator:cert", 1.0),
}
# META kinds reused with meta.py's value form and group, so URL ids match .info.json ids
META_GROUPS = {"platform_ad_id": "META:operator:platform_ad_id", "platform_page_id": "META:operator:platform_page_id"}

# One-line Polish bit-waterfall labels (KIND_PL lives in hydra/synapse.py; CR-OPGENES-6 asks to merge these).
KIND_PL_CERT: dict[str, str] = {
    "app_package_id": "Ta sama promowana aplikacja (identyfikator pakietu w sklepie)",
    "app_developer_id": "Ten sam deweloper aplikacji w sklepie",
    "platform_page_id": "Ten sam profil reklamodawcy (identyfikator strony Meta)",
    "premium_sms": "Ten sam numer SMS Premium i słowo kluczowe",
    "premium_sms_code": "Ten sam numer skrócony SMS Premium (wspólny dla wielu usług)",
    "carrier_billing_marker": "Ten sam sposób obciążenia rachunku telefonu (płatność przez operatora)",
    "cloud_lb_host": "Ta sama usługa balansera chmurowego (dostawca i region)",
    "cloud_lb_name": "Ten sam balanser chmurowy (instancja jednego klienta)",
    "tls_cert_sha256": "Ten sam certyfikat TLS (SHA-256) na różnych serwerach",
}

# ============================================================================================ app-store ids

_PKG = r"([A-Za-z][A-Za-z0-9_]{0,63}(?:\.[A-Za-z0-9_]{1,64}){1,12})"
_Q = r"/?\?(?:[^\s\"'<>#]*?&(?:amp;)?)?"
_APP_RX: list[tuple[str, str, re.Pattern[str]]] = [
    ("app_package_id", "android", re.compile(r"play\.google\.com/store/apps/details" + _Q + r"id=" + _PKG, re.I)),
    ("app_package_id", "android", re.compile(r"market://details" + _Q + r"id=" + _PKG, re.I)),
    ("app_package_id", "android", re.compile(r"intent:[^\s\"'<>]{0,300}?#Intent;[^\s\"'<>]{0,300}?\bpackage=" + _PKG,
                                              re.I)),
    ("app_package_id", "ios", re.compile(
        r"(?:apps|itunes)\.apple\.com/(?:[a-z]{2}/)?app/(?:[^/\s\"'<>?#]{1,120}/)?id(\d{6,12})", re.I)),
    ("app_developer_id", "play_dev", re.compile(r"play\.google\.com/store/apps/dev" + _Q + r"id=(\d{6,25})", re.I)),
    ("app_developer_id", "play_dev_name", re.compile(
        r"play\.google\.com/store/apps/developer" + _Q + r"id=([^\s\"'<>&#]{2,100})", re.I)),
    ("app_developer_id", "ios_dev", re.compile(
        r"(?:apps|itunes)\.apple\.com/(?:[a-z]{2}/)?developer/(?:[^/\s\"'<>?#]{1,120}/)?id(\d{6,12})", re.I)),
]
# Packages every page links to (browser / store / social apps; intent:// escapes from in-app browsers): 0 bits.
COMMON_PACKAGES = (
    "com.android.", "com.google.android.", "com.facebook.", "com.instagram.", "com.whatsapp", "org.telegram.",
    "com.zhiliaoapp.", "com.ss.android.", "com.sec.android.", "org.mozilla.", "com.opera.", "com.microsoft.",
    "com.apple.", "com.brave.", "com.UCMobile", "com.huawei.", "com.mi.", "com.miui.", "com.samsung.",
)
# Apps of brands that scam pages impersonate (banks, shops, couriers, public services): a copied footer badge
# links to the brand's real app, so the package says "same brand", not "same operator" -> kit. Assumed list.
BRAND_PACKAGES = (
    "pl.pkobp.", "com.pkobp.", "pl.mbank", "pl.ing.", "pl.pekao", "softax.pekao", "com.aliorbank", "pl.aliorbank",
    "pl.santander", "pl.bzwbk", "wit.android.bcpBankingApp.millenium", "pl.millennium", "pl.bnpparibas",
    "pl.bnp.", "pl.creditagricole", "pl.velobank", "pl.nestbank", "eu.eleader.mobilebanking", "pl.allegro",
    "pl.inpost", "pl.olx", "com.olx.", "pl.gov.", "pl.poczta", "pl.orange", "pl.play.", "pl.tmobile", "pl.plus",
    "com.revolut.", "com.paypal.", "pl.blik", "com.vinted.", "com.amazon.", "pl.dpd", "pl.dhl",
)


def _pkg_tier(pkg: str) -> tuple[str | None, str | None]:
    """(tier override, reason) for a package id; (None, None) = operator."""
    if pkg.startswith(COMMON_PACKAGES):
        return "boilerplate", "aplikacja systemowa / społecznościowa (link w wielu stronach)"
    if pkg.startswith(BRAND_PACKAGES):
        return "kit", "aplikacja podszywanej marki (np. baner banku w stopce)"
    return None, None


def _decoded_views(text: str) -> list[str]:
    """The text itself plus one percent-decoded and one HTML-unescaped view (redirect params, href attributes)."""
    views = [text]
    if "%2f" in text.lower() or "%3d" in text.lower() or "%3f" in text.lower():
        views.append(unquote(text))
    if "&amp;" in text or "&#" in text:
        views.append(html.unescape(text))
    return views


def find_app_ids(text: str) -> list[Entity]:
    """Store package ids and developer ids in URLs or text (defanged indicators are refanged first)."""
    out: list[Entity] = []
    seen: set[tuple[str, str]] = set()
    for vi, view in enumerate(_decoded_views(text)):
        t, om = refang(view)
        for kind, store, rx in _APP_RX:
            for m in rx.finditer(t):
                raw = m.group(1)
                tier, reason = None, None
                if store == "android":
                    raw = raw.rstrip("._")
                    if raw.count(".") < 1:
                        continue
                    tier, reason = _pkg_tier(raw)
                    value = f"android:{raw}"
                elif store == "ios":
                    value = f"ios:id{raw}"
                elif store == "play_dev_name":
                    value = "play_dev_name:" + " ".join(unquote_plus(raw).lower().split())
                else:
                    value = f"{store}:{raw}"
                if (kind, value) in seen:
                    continue
                seen.add((kind, value))
                s, e = orig_span(om, m.start(), m.end()) if vi == 0 else (0, 0)
                group = "app_package" if kind == "app_package_id" else "app_developer"
                q = 0.7 if store == "play_dev_name" else 1.0
                note = reason or ("adres sklepu z aplikacjami" + ("" if vi == 0 else " (zdekodowany)"))
                out.append(Entity(kind, value, s, e, q, note, tier=tier or "operator", group=group))
    return out


# ============================================================================================ Meta Ad Library ids

_ADLIB = re.compile(r"(?:facebook|fb)\.com/ads/(?:library|archive/render_ad)/?\?([^\s\"'<>#]{1,600})", re.I)


def find_adlibrary_ids(text: str) -> list[Entity]:
    """Ad Library URLs: ?id=<ad archive id> -> platform_ad_id; view_all_page_id / page_id -> platform_page_id.
    Values are the bare digits, as meta.py stores ids from yt-dlp .info.json."""
    out: list[Entity] = []
    seen: set[tuple[str, str]] = set()
    exact = "&amp;" not in text  # HTML-unescaping shifts offsets: such matches keep no character span
    t, om = refang(text if exact else html.unescape(text))
    for m in _ADLIB.finditer(t):
        for k, v in parse_qsl(m.group(1), keep_blank_values=False):
            k = k.lower()
            if not v.isdigit() or not 5 <= len(v) <= 20:
                continue
            kind = "platform_ad_id" if k == "id" else "platform_page_id" if k in ("view_all_page_id", "page_id") \
                else None
            if kind is None or (kind, v) in seen:
                continue
            seen.add((kind, v))
            s, e = orig_span(om, m.start(), m.end()) if exact else (0, 0)
            out.append(Entity(kind, v, s, e, 1.0 if kind == "platform_page_id" else 0.9,
                              f"adres Biblioteki reklam Meta ({k})", tier="operator", group=kind))
    return out


# ============================================================================================ premium-rate SMS

# Polish premium-rate SMS short codes (assumption, documented in docs/genes/INF.md section 11): 4 digits starting
# with 7 or 8, or 5 digits starting with 7, 8 or 9 (the UKE numbering plan prices by the leading digits; e.g.
# 92505 / 92512 / 92513 cost 30.75 PLN gross per CERT Polska 2026-09-23). Not checked against the UKE register.
_CODE = r"(?<![\d+])((?:[78]\d{3}|[789]\d{4}))(?![\d])"
_SMSW = r"(?:sms(?:-?a|-?em|y)?|wiadomo(?:ść|sc|ści|sci|ścią|scia)|smsa)"
_NUMW = r"(?:na|pod)\s+(?:(?:numer|nr\.?|numerem)\s+)?"
_TREW = r"(?:o\s+tre[sś]ci|z\s+tre[sś]ci[aą]|tre[sś][cć]|z\s+(?:kodem|has[lł]em)|o\s+brzmieniu|text)"
_KW = r"[\"„“”'«»]?([A-Za-z][A-Za-z0-9]{1,19}(?:[ .][A-Za-z0-9]{1,12}){0,2}?)[\"„“”'«»]?"
_SMS_RX: list[re.Pattern[str]] = [
    # "wyślij SMS o treści KEYWORD na numer 92513"
    re.compile(r"\b(?:wy[sś]l\w*|send|text)\s+(?:" + _SMSW + r"\s+)?" + _TREW + r"\s*[:\-]?\s*" + _KW + r"\s+"
               + _NUMW + _CODE, re.I),
    # "wyślij KEYWORD na 7136" / "wyślij SMS KEYWORD na numer 7136"
    re.compile(r"\b(?:wy[sś]l\w*|send|text)\s+(?:" + _SMSW + r"\s+)?" + _KW + r"\s+" + _NUMW + _CODE, re.I),
    # "SMS na numer 92513 o treści KEYWORD"
    re.compile(r"\b" + _SMSW + r"\s+" + _NUMW + _CODE + r"\s*,?\s*" + _TREW + r"\s*[:\-]?\s*" + _KW, re.I),
]
_SMSW_RX = re.compile(r"\b" + _SMSW + r"\b", re.I)
_SMS_CODE_ONLY = re.compile(r"\b" + _SMSW + r"\b[^.\n]{0,40}?" + _NUMW + _CODE, re.I)
_SMS_URI = re.compile(r"\bsms:(?://)?\+?(\d{4,5})(?:[;,]?\?|[;?&]+)?(?:[^\s\"'<>]*?\bbody=([^\s\"'<>&]{1,80}))?",
                      re.I)
_CODE_FULL = re.compile(_CODE)
_KW_STOP = {"na", "pod", "o", "sms", "smsa", "numer", "nr", "tresci", "treści", "teraz", "kod", "kodem", "ten",
            "ta", "to", "jeden", "jedno", "wiadomosc", "wiadomość", "z", "do", "the", "a", "an"}


def _norm_keyword(kw: str) -> str | None:
    toks = [t for t in re.split(r"[ .]+", kw.strip()) if t]
    if not toks or toks[0].lower() in _KW_STOP:
        return None
    # generated per-victim parts (e.g. "ALL 64913": the digits differ per registration) are masked
    out = ["#" if (t.isdigit() and len(t) >= 3) else t.upper() for t in toks]
    return " ".join(out)


def find_premium_sms(text: str) -> list[Entity]:
    """Premium SMS instructions: code + keyword -> premium_sms (operator), the code -> premium_sms_code (platform)."""
    out: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, s: int, e: int, q: float, note: str) -> None:
        if (kind, value) in seen:
            return
        seen.add((kind, value))
        tier = "operator" if kind == "premium_sms" else "platform"
        group = "premium_sms" if kind == "premium_sms" else "premium_sms_code"
        out.append(Entity(kind, value, s, e, q, note, tier=tier, group=group))

    for i, rx in enumerate(_SMS_RX):
        for m in rx.finditer(text):
            kw, code = (m.group(1), m.group(2)) if i < 2 else (m.group(2), m.group(1))
            nk = _norm_keyword(kw)
            if nk is None:
                continue
            if i == 1 and not _SMSW_RX.search(m.group(0)) and not kw.split()[0].isupper():
                continue  # "wyślij formularz na 7000": without the word SMS only an uppercase keyword counts
            add("premium_sms", f"{code}:{nk}", m.start(), m.end(), 0.9, f"SMS „{kw}” na {code}")
            add("premium_sms_code", code, m.start(), m.end(), 0.8, "numer skrócony SMS Premium (usługa wspólna)")
    for m in _SMS_CODE_ONLY.finditer(text):
        add("premium_sms_code", m.group(1), m.start(), m.end(), 0.7, "numer skrócony SMS Premium (usługa wspólna)")
    for m in _SMS_URI.finditer(text):
        code = m.group(1)
        if not _CODE_FULL.fullmatch(code):
            continue
        body = unquote_plus(m.group(2) or "").strip()
        nk = _norm_keyword(body) if body and re.fullmatch(r"[A-Za-z][A-Za-z0-9 .]{0,40}", body) else None
        if nk:
            add("premium_sms", f"{code}:{nk}", m.start(), m.end(), 0.9, f"odnośnik sms: z treścią „{body}”")
        add("premium_sms_code", code, m.start(), m.end(), 0.8, "odnośnik sms: na numer skrócony")
    return out


# Direct Carrier Billing / premium subscription wording (kit tier: the billing style of the scheme).
_BILLING_RX: list[tuple[str, re.Pattern[str]]] = [
    ("operator_payment", re.compile(r"p[lł]atno[sś][cć]\w*\s+(?:przez|za\s+po[sś]rednictwem)\s+operatora", re.I)),
    ("added_to_phone_bill", re.compile(r"doliczon\w*\s+do\s+(?:rachunku|faktury)|obci[aą][zż]\w*\s+"
                                       r"(?:rachunk\w*|faktur\w*|konta?)\s+(?:telefon\w*|kom[oó]rkow\w*|"
                                       r"u\s+operatora)", re.I)),
    ("prepaid_balance", re.compile(r"(?:pobran\w*|odliczon\w*)\s+(?:ze?\s+)?(?:[sś]rodk[oó]w|salda)\s+"
                                   r"(?:na\s+)?(?:koncie|karcie|konta)?\s*(?:prepaid|telefon\w*)", re.I)),
    ("recurring_subscription", re.compile(
        r"subskrypcj\w*[^.\n]{0,60}?\d+(?:[,.]\d{1,2})?\s*z[lł]\s*(?:brutto\s*)?(?:/|za|co)\s*"
        r"(?:tydz\w*|7\s*dni|siedem\s+dni|dzie[nń]|dob\w*|mies\w*)", re.I)),
    ("premium_service", re.compile(r"\bus[lł]ug\w*\s+(?:sms\s+)?premium|\bsms\s+premium|\bpremium\s+sms", re.I)),
    ("sms_price_line", re.compile(r"koszt\w*\s+(?:sms|wiadomo[sś]ci|jednego\s+sms)\w*[^.\n]{0,20}?"
                                  r"\d+(?:[,.]\d{2})?\s*z[lł]\s*(?:z\s+vat|brutto)", re.I)),
    ("dcb_term", re.compile(r"\bdirect\s+carrier\s+billing\b|\bcarrier\s+billing\b|\bDCB\b")),
    ("msisdn_field", re.compile(r"\bmsisdn\b", re.I)),
]


def find_billing_markers(text: str) -> list[Entity]:
    out = []
    for name, rx in _BILLING_RX:
        m = rx.search(text)
        if m:
            out.append(Entity("carrier_billing_marker", name, m.start(), m.end(), 0.5,
                              "wzmianka o płatności przez operatora / usłudze premium", tier="kit",
                              group="carrier_billing"))
    return out


# ============================================================================================ TLS certificate in text

_CERT_HEX = re.compile(r"(?<![0-9a-f])([0-9a-f]{64}|(?:[0-9a-f]{2}:){31}[0-9a-f]{2})(?![0-9a-f:])", re.I)
_CERT_CTX = re.compile(r"certyfika\w*|certificate|\bcert\b|\btls\b|\bssl\b|x\.?509|fingerprint|odcisk", re.I)
_FILE_CTX = re.compile(r"plik\w*|file|apk|dex|payload|próbk\w*|sample|obraz\w*|materia\w*", re.I)


def find_cert_hashes(text: str) -> list[Entity]:
    """Certificate SHA-256 fingerprints in pasted reports ("Certificate SHA-256: <hex>"). Requires certificate
    wording within 60 characters before the hash and no file wording closer to it."""
    out = []
    for m in _CERT_HEX.finditer(text):
        before = text[max(0, m.start() - 60): m.start()]
        cm = list(_CERT_CTX.finditer(before))
        if not cm:
            continue
        fm = list(_FILE_CTX.finditer(before))
        if fm and fm[-1].start() > cm[-1].start():
            continue
        value = m.group(1).lower().replace(":", "")
        out.append(Entity("tls_cert_sha256", value, m.start(), m.end(), 0.9, "odcisk SHA-256 certyfikatu TLS",
                          tier="operator", group="cert"))
    return out


# ============================================================================================ cloud load balancers

# provider -> (hostname regex with an `inst` group (None = shared front door, no per-customer name), region group)
_LB_RX: list[tuple[str, re.Pattern[str]]] = [
    ("aliyun_alb", re.compile(r"^(?P<inst>alb-[a-z0-9]{6,40})\.(?:(?P<region>(?!alb\.)[a-z0-9-]+)\.)?(?:alb\.)?"
                              r"aliyuncs(?:slb)?(?:intl)?\.com$")),
    ("aliyun_nlb", re.compile(r"^(?P<inst>nlb-[a-z0-9]{6,40})\.(?:(?P<region>(?!nlb\.)[a-z0-9-]+)\.)?(?:nlb\.)?"
                              r"aliyuncs(?:slb)?(?:intl)?\.com$")),
    ("aliyun_slb", re.compile(r"^(?P<inst>(?:lb|slb)-[a-z0-9]{6,40})\.(?:(?P<region>[a-z0-9-]+)\.)?"
                              r"aliyuncsslb(?:intl)?\.com$")),
    ("aws_elb", re.compile(r"^(?P<inst>(?:internal-)?[a-z0-9-]{1,40}-\d{6,12})\.(?P<region>[a-z0-9-]+)"
                           r"\.elb\.amazonaws\.com$")),
    ("aws_nlb", re.compile(r"^(?P<inst>[a-z0-9-]{1,40}-[0-9a-f]{16})\.elb\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$")),
    ("aws_cloudfront", re.compile(r"^(?P<inst>d[a-z0-9]{12,14})\.cloudfront\.net$")),
    ("aws_global_accelerator", re.compile(r"^(?P<inst>[a-z0-9]{16,20})\.awsglobalaccelerator\.com$")),
    ("azure_frontdoor", re.compile(r"^(?P<inst>[a-z0-9-]{1,60}(?:-[a-z0-9]{16})?)\.(?:z\d{2}\.)?azurefd\.net$")),
    ("azure_cloudapp", re.compile(r"^(?P<inst>[a-z0-9-]{1,63})\.(?P<region>[a-z0-9]+)\.cloudapp\.azure\.com$")),
    ("azure_trafficmanager", re.compile(r"^(?P<inst>[a-z0-9-]{1,63})\.trafficmanager\.net$")),
    ("tencent_clb", re.compile(r"^(?P<inst>lb-[a-z0-9]{8}-[a-z0-9]{10,20})\.clb\.(?P<region>[a-z0-9-]+)"
                               r"-tencentclb\.(?:com|net|cloud)$")),
    ("gcp_run", re.compile(r"^(?P<inst>[a-z0-9-]{1,63}-[a-z0-9]{10}-[a-z]{2})\.a\.run\.app$")),
    # shared front doors: the name is generated per edge, not per customer -> platform only
    ("fastly", re.compile(r"^(?:[a-z0-9-]+\.)*(?:global\.ssl|map|freetls)\.fastly\.net$")),
    ("akamai", re.compile(r"^(?:[a-z0-9-]+\.)+(?:akamaiedge|edgekey|edgesuite|akamaized)\.net$")),
    ("cloudflare", re.compile(r"^(?:[a-z0-9-]+\.)+cdn\.cloudflare\.net$")),
]
SHARED_LB = {"fastly", "akamai", "cloudflare"}


def cloud_lb(host: str) -> tuple[str, str, str | None] | None:
    """(provider, region or "-", per-customer instance id or None) for a cloud load-balancer hostname."""
    h = host.lower().rstrip(".")
    for prov, rx in _LB_RX:
        m = rx.match(h)
        if not m:
            continue
        gd = m.groupdict()
        region = gd.get("region") or "-"
        inst = None if prov in SHARED_LB else gd.get("inst")
        return prov, region, inst
    return None


def lb_entities(host: str, start: int = 0, end: int = 0) -> list[Entity]:
    hit = cloud_lb(host)
    if hit is None:
        return []
    prov, region, inst = hit
    out = [Entity("cloud_lb_host", f"{prov}:{region}", start, end, 0.4, f"{host}: usługa balansera {prov}",
                  tier="platform", group="cloud_lb")]
    if inst:
        out.append(Entity("cloud_lb_name", f"{prov}:{inst}", start, end, 0.9,
                          f"{host}: instancja balansera przydzielona jednemu klientowi chmury", tier="operator",
                          group="cloud_lb_name"))
    return out


def find_cloud_lbs(text: str) -> list[Entity]:
    out = []
    for e in find_hosts(text):
        if e.kind == "domain_in_text":
            out += lb_entities(e.value, e.start, e.end)
    return out


def lb_genes(sink: GeneSink, host: str, **ev) -> None:
    """Add cloud_lb_host / cloud_lb_name genes for a hostname (HAR host, DNS CNAME target, linked host)."""
    note = ev.pop("note", None)
    for e in lb_entities(host):
        grp, q = TOLL_KINDS[e.kind]
        sink.add(e.kind, e.value, grp, quality=q, note=f"{note}: {e.note}" if note else e.note, **ev)


# ============================================================================================ shared certificates

# SAN suffixes of multi-tenant certificates (shared hosting / CDN / PaaS default domains): the same certificate on
# two incidents means the same provider, not the same operator.
SHARED_CERT_SUFFIXES = (
    "cloudflaressl.com", "cloudfront.net", "elb.amazonaws.com", "amazonaws.com", "aliyuncs.com",
    "aliyuncsslb.com", "aliyuncsslbintl.com", "azureedge.net", "azurewebsites.net", "azurefd.net",
    "herokuapp.com", "herokudns.com", "vercel.app", "netlify.app", "github.io", "pages.dev", "workers.dev",
    "firebaseapp.com", "web.app", "appspot.com", "run.app", "fastly.net", "akamaized.net", "akamaihd.net",
    "edgekey.net", "wpengine.com", "myshopify.com", "wixsite.com", "blogspot.com", "onrender.com", "fly.dev",
    "railway.app", "ngrok-free.app", "ngrok.io", "trycloudflare.com", "googleusercontent.com", "000webhostapp.com",
    "weebly.com", "squarespace.com", "webflow.io", "framer.app", "tilda.ws", "carrd.co",
)
MULTI_TENANT_SANS = 40  # more SAN names than this = shared certificate (old Cloudflare SNI certs ~50 customers)


def shared_cert_reason(host: str, info: dict) -> str | None:
    """Why a served certificate is not operator evidence (None = it is)."""
    if is_boilerplate(host):
        return "certyfikat usługi wspólnej (host z listy boilerplate)"
    sans = [str(s).lower().removeprefix("*.") for s in info.get("sans") or []]
    if len(sans) > MULTI_TENANT_SANS:
        return f"certyfikat wielu klientów ({len(sans)} nazw SAN)"
    for s in sans:
        if any(s == suf or s.endswith("." + suf) for suf in SHARED_CERT_SUFFIXES):
            return f"certyfikat domeny dostawcy ({s})"
    return None


# ============================================================================================ TXT entry point


def find_text_ids(text: str) -> list[Entity]:
    """All finders of this module for the TXT extractor (operator_ids.find_operator_ids)."""
    return (find_app_ids(text) + find_adlibrary_ids(text) + find_premium_sms(text) + find_billing_markers(text)
            + find_cert_hashes(text) + find_cloud_lbs(text))


# ============================================================================================ INF extractor


def _meta_gene(inc_id: str, e: Entity, **ev) -> Gene:
    return Gene(incident_id=inc_id, family=Family.META, kind=e.kind, value=e.value, quality=e.quality,
                status=GeneStatus.OBSERVED, provenance=Provenance.OBS, extractor=EXTRACTOR,
                dependency_group=META_GROUPS[e.kind], evidence=EvidencePointer(note=e.note, **ev))


STORE_HOSTS = ("play.google.com", "apps.apple.com", "itunes.apple.com")


@register("inf_tollfraud", family=Family.INF, version=1)
def inf_tollfraud(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """App-store ids, Ad Library ids, premium SMS, carrier-billing wording and cloud load balancers in the saved
    web material (landing URLs, HAR request / redirect URLs and bodies, HTML source and text, saved assets)."""
    sink = GeneSink(inc.incident.id, EXTRACTOR)
    meta: list[Gene] = []
    meta_seen: set[tuple[str, str]] = set()
    b = bundle(inc)

    def ids(text: str, label: str, *, billing_text: str | None = None, **ev) -> None:
        text = text[:MAX_TEXT_SCAN]
        ents = find_app_ids(text) + find_adlibrary_ids(text) + find_premium_sms(text)
        if billing_text is not None:
            ents += find_billing_markers(billing_text[:MAX_TEXT_SCAN]) + find_premium_sms(billing_text[:MAX_TEXT_SCAN])
        for e in ents:
            if e.kind in META_GROUPS:
                if (e.kind, e.value) not in meta_seen:
                    meta_seen.add((e.kind, e.value))
                    meta.append(_meta_gene(inc.incident.id, e, artifact_id=ev.get("artifact_id"),
                                           har_entry=ev.get("har_entry"), dom_path=ev.get("dom_path")))
                continue
            grp, q = TOLL_KINDS[e.kind]
            q = min(q, e.quality)
            if e.tier and e.tier != grp.split(":")[1]:  # well-known / brand app package: tier lowered
                grp, q = f"inf:{e.tier}:{grp.split(':')[-1]}", min(q, 0.3)
            sink.add(e.kind, e.value, grp, quality=q, note=f"{label}: {e.note}" + (f" @{e.start}" if e.end else ""),
                     **ev)

    for url, src, aid in b.landing_urls:
        ids(url, f"adres docelowy ({src})", artifact_id=aid)
        host = (urlsplit(url).hostname or "").lower()
        if valid_host(host):
            lb_genes(sink, host, artifact_id=aid, note=f"host adresu docelowego ({src})")

    for h in b.hars:
        if h.error or h.local_replay:
            continue
        aid = h.artifact_id
        for host, hi in h.hosts.items():
            lb_genes(sink, host, artifact_id=aid, har_entry=hi.first_entry, note="host w HAR")
        for r in h.requests:
            ids(str(r.get("url", "")), f"żądanie HAR {r.get('entry')}", artifact_id=aid, har_entry=r.get("entry"))
        for rd in h.redirects:
            ids(str(rd.get("to", "")), f"przekierowanie HAR {rd.get('entry')}", artifact_id=aid,
                har_entry=rd.get("entry"))
        for body in h.bodies:
            host = (urlsplit(body.url).hostname or "").lower()
            if not (is_text_mime(body.mime) or body.url.endswith("#post")):
                continue
            txt = body.data.decode("utf-8", "replace")
            if host in STORE_HOSTS:
                # a store page lists other ("similar") apps: keep only an unambiguous developer link
                devs = {e.value for e in find_app_ids(txt[:MAX_TEXT_SCAN]) if e.kind == "app_developer_id"}
                if len(devs) == 1:
                    grp, q = TOLL_KINDS["app_developer_id"]
                    sink.add("app_developer_id", devs.pop(), grp, artifact_id=aid, har_entry=body.entry,
                             quality=q, note="strona aplikacji w sklepie: link do dewelopera")
                continue
            if is_boilerplate(host):
                continue
            ids(txt, f"treść HAR {body.entry}", artifact_id=aid, har_entry=body.entry,
                billing_text=txt if body.mime == "text/html" else None)
            for m in URL_HOST_RX.findall(txt[:MAX_TEXT_SCAN]):
                hh = m.lower().strip(".")
                if valid_host(hh):
                    lb_genes(sink, hh, artifact_id=aid, har_entry=body.entry, note="host w treści HAR")

    for d in b.htmls:
        if d.error:
            continue
        ids(d.raw, "źródło strony", billing_text=d.text, artifact_id=d.artifact_id, dom_path="html")
        for m in URL_HOST_RX.findall(d.raw[:MAX_TEXT_SCAN]):
            hh = m.lower().strip(".")
            if valid_host(hh):
                lb_genes(sink, hh, artifact_id=d.artifact_id, dom_path="html", note="host w kodzie strony")

    for a, data in b.assets:
        low = a.meta.path_in_incident.lower().split(".pobrane")[0]
        if not low.endswith((".js", ".mjs", ".html", ".htm", ".json", ".txt")):
            continue
        ids(data.decode("utf-8", "replace"), a.meta.path_in_incident.rsplit("/", 1)[-1][:80],
            artifact_id=a.meta.artifact_id, dom_path=a.meta.path_in_incident[-200:])

    res = sink.result()
    res.genes.extend(meta)
    return res
