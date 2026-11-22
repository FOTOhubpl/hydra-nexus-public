"""INF genes for operator exfiltration endpoints and kit identifiers found in page scripts and kit source.

Clean-room implementation from docs/cleanroom/kit_exfil_indicators.md and docs/cleanroom/iok_kit_rules.md
plus public format documentation (Telegram Bot API, Discord webhook URL format, reCAPTCHA / hCaptcha /
Cloudflare Turnstile docs). No third-party scanner code is used, copied or paraphrased.

What it finds (one extractor `inf_kit`, static text only — never executes code, never fetches):
  * Telegram bot token (stored as a salted hash) + the non-secret numeric bot id, and chat id -> operator.
  * Discord webhook (numeric id kept, token hashed) -> operator.
  * Mail drops in kit source (mail()/mailer recipient resolved through one variable assignment) -> operator.
  * Captcha sitekeys (reCAPTCHA / hCaptcha / Turnstile); vendor public test keys -> boilerplate (0 bits).
  * Kit author credits ("coded/made/... by <handle>", @author, ICQ/Skype) -> kit.
  * Kit admin/panel/gate endpoints in first-party request URLs or code -> kit.
Optionally, `kit_rules` (YAML rule engine) emits `kit_rule` genes from the same extractor.

Raw bot/webhook tokens are never stored: values and notes carry only hashes or non-secret ids. `redact.py`
strips any raw token that still reaches an export.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from hydra.contracts import Family
from hydra.enrich.domains import is_boilerplate
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.webparse import (
    MAX_TEXT_SCAN,
    GeneSink,
    bundle,
    first_party_hosts,
    pii_hash,
)

# ------------------------------------------------------------------------------------------- tiers/groups

# kind -> (dependency group, starting quality). Vendor test sitekeys override the group to boilerplate.
KIT_KINDS: dict[str, tuple[str, float]] = {
    "exfil_telegram_bot": ("inf:operator:exfil_telegram", 1.0),     # salted hash of <id>:<token>
    "exfil_telegram_botid": ("inf:operator:exfil_telegram", 0.9),   # numeric bot id (not secret)
    "exfil_telegram_chat": ("inf:operator:exfil_telegram", 1.0),    # signed chat id
    "exfil_discord_webhook": ("inf:operator:exfil_discord", 1.0),   # numeric webhook id
    "exfil_discord_token": ("inf:operator:exfil_discord", 1.0),     # salted hash of the token
    "mail_drop": ("inf:operator:contact", 0.8),                     # salted hash of the e-mail
    "captcha_sitekey": ("inf:operator:captcha", 0.9),               # "<vendor>:<sitekey>"
    "kit_author": ("inf:kit:author", 0.5),                          # normalised handle
    "kit_panel_path": ("inf:kit:panel_path", 0.6),                  # admin/panel/gate endpoint
    "kit_rule": ("inf:kit:kit_rule", 1.0),                          # named rule match (rule id)
    # GAPS_PAPERS G07: third-party account ids embedded in the page (each bound to one customer account)
    "chat_widget_id": ("inf:operator:chat_widget", 0.9),            # "<vendor>:<property/licence id>"
    "payment_processor_id": ("inf:operator:payment_processor", 0.9),  # "<vendor>:<public id>"
    "ad_publisher_id": ("inf:operator:ad_publisher", 0.9),          # "adsense:ca-pub-…" / ads.txt seller
}

# One-line Polish bit-waterfall labels (KIND_PL lives in synapse.py, which this module must not edit).
KIND_PL_KIT: dict[str, str] = {
    "exfil_telegram_bot": "Ten sam bot Telegram do wysyłki wykradzionych danych",
    "exfil_telegram_botid": "Ten sam identyfikator bota Telegram",
    "exfil_telegram_chat": "Ten sam czat Telegram odbierający dane",
    "exfil_discord_webhook": "Ten sam webhook Discord do wysyłki danych",
    "exfil_discord_token": "Ten sam sekret webhooka Discord",
    "mail_drop": "Ten sam adres e-mail odbierający dane (mail drop)",
    "captcha_sitekey": "Ten sam klucz captcha (to samo konto operatora)",
    "kit_author": "Ten sam podpis autora zestawu (kit)",
    "kit_panel_path": "Ta sama ścieżka panelu zestawu (kit)",
    "kit_rule": "Dopasowano regułę rozpoznawania zestawu (kit)",
    "chat_widget_id": "To samo konto widżetu czatu na żywo (np. tawk.to, Smartsupp)",
    "payment_processor_id": "To samo konto operatora płatności (np. PayPal, Stripe, merchant id)",
    "ad_publisher_id": "To samo konto wydawcy reklam (np. AdSense ca-pub)",
}

# ------------------------------------------------------------------------------------------- allowlists

# Bundled-library / test-framework path fragments: these carry library authors' e-mails and credits, not the
# operator's. Written from the format description, not copied from any scanner.
_LIB_PATH = re.compile(
    r"(?:^|/)(?:vendor|node_modules|bower_components|third[_-]?party|3rdparty|dist|build|"
    r"jquery|bootstrap|react|vue|lodash|moment|swiper|slick|owl\.?carousel|gsap|axios|"
    r"phpmailer|swiftmailer|symfony|laravel|composer|packages|site-packages|"
    r"__tests__|tests?|spec|fixtures?|mocks?|examples?|polyfill|"
    r"fontawesome|font-?awesome|animate|sweetalert|intl-tel-input|imask|inputmask)[/.\-]",
    re.I,
)

# Vendor-published public captcha test sitekeys (from vendor docs); shared by demos, so 0 bits.
CAPTCHA_TEST_KEYS = {
    "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",   # Google reCAPTCHA v2 test site key
    "6LdyC2cUAAAAACS-qK95Ul5Xf8wuGfFPJ_UMAX1A",   # Google reCAPTCHA (alt documented test key)
    "10000000-ffff-ffff-ffff-000000000001",       # hCaptcha test sitekey
    "20000000-ffff-ffff-ffff-000000000002",       # hCaptcha enterprise test sitekey
    "1x00000000000000000000AA",                    # Turnstile always-passes
    "2x00000000000000000000AB",                    # Turnstile always-blocks
    "3x00000000000000000000FF",                    # Turnstile forces interactive
    "1x00000000000000000000BB",                    # Turnstile invisible always-passes
}

# Author-credit false positives (generic words, tools, placeholders): never a handle.
_AUTHOR_FP = {
    "the", "me", "us", "you", "him", "her", "team", "admin", "root", "user", "name", "yourname",
    "default", "unknown", "anonymous", "author", "hand", "myself", "someone", "google", "facebook",
    "jquery", "bootstrap", "react", "vue", "wordpress", "php", "html", "css", "js", "code", "design",
    "yoast", "elementor", "a", "an", "and", "or",
}

# Placeholder mail-drop domains (dropped). Free-mail providers are kept.
_PLACEHOLDER_MAIL = re.compile(
    r"@(?:example\.(?:com|org|net)|example|your-?domain|your-?site|yourdomain|domain\.(?:com|tld)|"
    r"localhost|test(?:\.(?:com|local))?|email\.com|mail\.com|sample\.com|changeme|email\.tld)$",
    re.I,
)

# ------------------------------------------------------------------------------------------- patterns

# Telegram bot token: <account 8-12 digits>:<30-45 url-safe chars> on word boundaries (Bot API format).
_TG_TOKEN = re.compile(r"(?<![\w-])(\d{8,12}):([A-Za-z0-9_-]{30,45})(?![A-Za-z0-9_-])")
_TG_TOKEN_URL = re.compile(r"api\.telegram\.org/bot(\d{8,12}):([A-Za-z0-9_-]{30,45})", re.I)
_TG_HOST = re.compile(r"api\.telegram\.org|(?<![\w.])t\.me/", re.I)
# chat_id = / : optional-quote signed 6-13 digits; also as a Telegram API query parameter.
_TG_CHAT = re.compile(r"""chat_?id\s*["']?\s*[:=]\s*["']?\s*(-?\d{6,13})(?!\d)""", re.I)
_TG_CHAT_Q = re.compile(r"[?&]chat_?id=(-?\d{6,13})(?!\d)", re.I)

# Discord webhook URL: optional ptb/canary subdomain, api, optional version, webhooks, id, token.
_DISCORD = re.compile(
    r"https?://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api(?:/v\d+)?/webhooks/(\d{17,20})/"
    r"([A-Za-z0-9_.\-]{50,90})",
    re.I,
)

# Mail-sending calls (kit source). First argument is the recipient (literal or one variable).
_MAIL_CALL = re.compile(
    r"(?:\bmail\s*\(|->\s*(?:addAddress|AddAddress|addRecipient|addTo|setTo|to)\s*\(|"
    r"\bwp_mail\s*\()\s*([^,)\n]{1,120})",
    re.I,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_PHP_VAR = re.compile(r"\$[A-Za-z_]\w*")

# Captcha widgets: class + data-sitekey; and api-script render=/sitekey param.
_CAPTCHA_WIDGET = re.compile(
    r"class\s*=\s*[\"'][^\"']*\b(g-recaptcha|h-captcha|cf-turnstile)\b[^\"']*[\"'][^>]*?"
    r"data-sitekey\s*=\s*[\"']([^\"']{6,120})[\"']",
    re.I | re.S,
)
_CAPTCHA_WIDGET_REV = re.compile(
    r"data-sitekey\s*=\s*[\"']([^\"']{6,120})[\"'][^>]*?class\s*=\s*[\"'][^\"']*"
    r"\b(g-recaptcha|h-captcha|cf-turnstile)\b",
    re.I | re.S,
)
_CAPTCHA_SCRIPT = re.compile(
    r"(recaptcha/api\.js|hcaptcha\.com/\d+/api\.js|challenges\.cloudflare\.com/turnstile/[^\"'?\s]*api\.js)"
    r"[^\"'\s]*[?&](?:render|sitekey)=([A-Za-z0-9_\-]{6,120})",
    re.I,
)
_VENDOR_BY_CLASS = {"g-recaptcha": "recaptcha", "h-captcha": "hcaptcha", "cf-turnstile": "turnstile"}

# Author credits.
_CREDIT = re.compile(
    r"\b(?:coded|made|developed|created|designed|programmed|written|built)\s+by\s*[:\-]?\s*"
    r"([@A-Za-z0-9_.\-]{3,30})",
    re.I,
)
_AUTHOR_TAG = re.compile(r"@author\s*[:\-]?\s*([@A-Za-z0-9_.\- ]{3,30})", re.I)
_ICQ = re.compile(r"\bICQ\b\s*[:#]?\s*(\d{6,10})", re.I)
_SKYPE = re.compile(r"\bskype\b\s*[:]?\s*(?:live:)?([A-Za-z][A-Za-z0-9_.\-]{2,30})", re.I)
# Library licence-header markers near a credit -> not an operator/kit author (jQuery etc.).
_LICENCE_CTX = re.compile(
    r"permission is hereby granted|copyright\s*\(c\)|\bMIT License\b|licensed under|@license|"
    r"all rights reserved|redistribution|jquery foundation|openjs foundation|the software is provided",
    re.I,
)

# Kit admin/panel/gate endpoints.
_PANEL_PATH = re.compile(
    r"/((?:admin|panel|gate|cpanel|controlpanel|backend|dashboard|manage|login|result|results|"
    r"next|antibots?|allow)[A-Za-z0-9_\-]*\.php)\b",
    re.I,
)

# Third-party account ids (G07). Written from the vendors' public embed snippets (format only, [U] until checked
# against current vendor docs): tawk.to embed path, Smartsupp key, JivoChat widget path, LiveChat licence, Crisp
# website id, PayPal JS SDK client-id, Stripe publishable key, AdSense publisher id, ads.txt seller lines.
_WIDGET_RX: list[tuple[str, str, re.Pattern[str]]] = [
    ("chat_widget_id", "tawk", re.compile(r"embed\.tawk\.to/([0-9a-f]{24})(?:/([A-Za-z0-9]{6,20}))?", re.I)),
    ("chat_widget_id", "smartsupp", re.compile(r"_smartsupp\.key\s*=\s*[\"']([0-9a-f]{40})[\"']", re.I)),
    ("chat_widget_id", "jivo", re.compile(r"code\.jivo(?:site)?\.com/(?:script/)?widget/([A-Za-z0-9]{6,16})", re.I)),
    ("chat_widget_id", "livechat", re.compile(r"__lc\.license\s*=\s*(\d{6,10})", re.I)),
    ("chat_widget_id", "crisp", re.compile(
        r"CRISP_WEBSITE_ID\s*=\s*[\"']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[\"']", re.I)),
    ("payment_processor_id", "paypal", re.compile(
        r"paypal\.com/sdk/js\?(?:[^\"'\s]*?&(?:amp;)?)?client-id=([A-Za-z0-9_-]{2,100})", re.I)),
    ("payment_processor_id", "stripe", re.compile(r"(?<![A-Za-z0-9_])(pk_(?:live|test)_[A-Za-z0-9]{16,120})")),
    ("ad_publisher_id", "adsense", re.compile(r"(?<![A-Za-z0-9-])(ca-pub-\d{16})(?!\d)")),
]
_ADS_TXT = re.compile(r"(?m)^\s*([a-z0-9.-]+\.[a-z]{2,})\s*,\s*([A-Za-z0-9-]{4,40})\s*,\s*(DIRECT|RESELLER)\b", re.I)
_MERCHANT_FIELD = re.compile(
    r"<input\b[^>]*?\bname\s*=\s*[\"']((?:p24_)?(?:merchant_?id|merchant|pos_?id|shop_?id|seller_?id|"
    r"merchant_?code|terminal_?id))[\"'][^>]*?\bvalue\s*=\s*[\"']([^\"']{2,64})[\"']", re.I)
_MERCHANT_FIELD_REV = re.compile(
    r"<input\b[^>]*?\bvalue\s*=\s*[\"']([^\"']{2,64})[\"'][^>]*?\bname\s*=\s*[\"']((?:p24_)?(?:merchant_?id|merchant|"
    r"pos_?id|shop_?id|seller_?id|merchant_?code|terminal_?id))[\"']", re.I)
_PLACEHOLDER_ID = re.compile(r"^(?:0+|1234567890123456|x+|test|demo|sandbox|sb|your[_-]?\w*|\{\{.*\}\}|<.*>)$", re.I)


def find_account_widgets(text: str) -> list[Hit]:
    """Chat-widget, payment-processor and ad-publisher account ids (operator tier). Public test / sandbox keys and
    placeholders are boilerplate (0 bits). Stripe secret keys never reach a gene (`redact.py` removes them)."""
    out: list[Hit] = []
    for kind, vendor, rx in _WIDGET_RX:
        for m in rx.finditer(text):
            val = m.group(1)
            test = bool(_PLACEHOLDER_ID.match(val)) or val.startswith("pk_test_") or \
                (vendor == "adsense" and _PLACEHOLDER_ID.match(val[7:]) is not None)
            extra = f"/{m.group(2)}" if vendor == "tawk" and m.lastindex and m.lastindex >= 2 and m.group(2) else ""
            note = f"{vendor}{' (klucz testowy/sandbox)' if test else ''} (offset {m.start()})"
            out.append(Hit(kind, f"{vendor}:{val}{extra}", m.start(), note, boilerplate=test))
    for m in _ADS_TXT.finditer(text):
        dom, acct = m.group(1).lower(), m.group(2)
        out.append(Hit("ad_publisher_id", f"adstxt:{dom}:{acct}", m.start(), f"ads.txt {dom} (offset {m.start()})"))
    for rx, order in ((_MERCHANT_FIELD, "nv"), (_MERCHANT_FIELD_REV, "vn")):
        for m in rx.finditer(text):
            name, val = (m.group(1), m.group(2)) if order == "nv" else (m.group(2), m.group(1))
            test = bool(_PLACEHOLDER_ID.match(val))
            out.append(Hit("payment_processor_id", f"merchant_field:{name.lower()}={val}", m.start(),
                           f"ukryte pole {name} (offset {m.start()})", boilerplate=test))
    return _dedup(out)


_B64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,200}={0,2}(?![A-Za-z0-9+/])")
_CONCAT = re.compile(r"[\"']\s*\+\s*[\"']")


# ------------------------------------------------------------------------------------------- hit model


@dataclass
class Hit:
    kind: str
    value: str
    offset: int
    note: str
    boilerplate: bool = False  # vendor test sitekey -> boilerplate tier, 0 bits


def _is_lib_path(path: str) -> bool:
    return bool(path) and bool(_LIB_PATH.search(path))


# ------------------------------------------------------------------------------------------- finders


def find_telegram(text: str, *, telegram_context: bool) -> list[Hit]:
    """Bot tokens (hashed) + bot id, and chat ids. Chat ids from JS need a Telegram context in the body."""
    out: list[Hit] = []
    seen_tokens: set[str] = set()
    for rx in (_TG_TOKEN, _TG_TOKEN_URL):
        for m in rx.finditer(text):
            bot_id, token = m.group(1), m.group(2)
            full = f"{bot_id}:{token}"
            if full in seen_tokens:
                continue
            seen_tokens.add(full)
            out.append(Hit("exfil_telegram_bot", pii_hash(full), m.start(),
                           f"Telegram bot {bot_id}, token zahaszowany (offset {m.start()})"))
            out.append(Hit("exfil_telegram_botid", bot_id, m.start(),
                           f"id bota Telegram {bot_id} (offset {m.start()})"))
    has_tg = telegram_context or bool(seen_tokens) or bool(_TG_HOST.search(text))
    for m in _TG_CHAT_Q.finditer(text):
        out.append(Hit("exfil_telegram_chat", m.group(1), m.start(),
                       f"chat_id {m.group(1)} w adresie API Telegrama (offset {m.start()})"))
    if has_tg:
        for m in _TG_CHAT.finditer(text):
            out.append(Hit("exfil_telegram_chat", m.group(1), m.start(),
                           f"chat_id {m.group(1)} (offset {m.start()})"))
    return _dedup(out)


def find_discord(text: str) -> list[Hit]:
    out: list[Hit] = []
    for m in _DISCORD.finditer(text):
        wid, token = m.group(1), m.group(2)
        out.append(Hit("exfil_discord_webhook", wid, m.start(),
                       f"webhook Discord id {wid} (offset {m.start()})"))
        out.append(Hit("exfil_discord_token", pii_hash(f"{wid}/{token}"), m.start(),
                       f"sekret webhooka Discord zahaszowany (offset {m.start()})"))
    return _dedup(out)


def find_mail_drops(text: str) -> list[Hit]:
    """Recipient of mail()/mailer setters, resolving one level of simple variable assignment."""
    out: list[Hit] = []
    for m in _MAIL_CALL.finditer(text):
        arg = m.group(1).strip()
        emails: list[str] = []
        lit = _EMAIL.findall(arg)
        if lit:
            emails = lit
        else:
            var = _PHP_VAR.search(arg)
            if var:
                assign = re.compile(re.escape(var.group(0)) + r"\s*=\s*[\"']([^\"']+)[\"']")
                for a in assign.finditer(text):
                    emails.extend(_EMAIL.findall(a.group(1)))
        for e in emails:
            e = e.lower()
            if _PLACEHOLDER_MAIL.search(e):
                continue
            user, _, dom = e.partition("@")
            out.append(Hit("mail_drop", pii_hash(e), m.start(),
                           f"mail drop {user[:1]}***@{dom} (offset {m.start()})"))
    return _dedup(out)


def find_sitekeys(text: str) -> list[Hit]:
    out: list[Hit] = []
    for rx, order in ((_CAPTCHA_WIDGET, "cv"), (_CAPTCHA_WIDGET_REV, "vc")):
        for m in rx.finditer(text):
            cls, key = (m.group(1), m.group(2)) if order == "cv" else (m.group(2), m.group(1))
            vendor = _VENDOR_BY_CLASS.get(cls.lower(), "captcha")
            out.append(_sitekey_hit(vendor, key, m.start()))
    for m in _CAPTCHA_SCRIPT.finditer(text):
        src, key = m.group(1).lower(), m.group(2)
        vendor = ("recaptcha" if "recaptcha" in src else "hcaptcha" if "hcaptcha" in src else "turnstile")
        out.append(_sitekey_hit(vendor, key, m.start()))
    return _dedup(out)


def _sitekey_hit(vendor: str, key: str, offset: int) -> Hit:
    test = key in CAPTCHA_TEST_KEYS
    note = f"klucz {vendor}{' (publiczny klucz testowy)' if test else ''} (offset {offset})"
    return Hit("captcha_sitekey", f"{vendor}:{key}", offset, note, boilerplate=test)


def find_authors(text: str) -> list[Hit]:
    out: list[Hit] = []

    def near_licence(pos: int) -> bool:
        return bool(_LICENCE_CTX.search(text[max(0, pos - 220): pos + 220]))

    for rx in (_CREDIT, _AUTHOR_TAG):
        for m in rx.finditer(text):
            handle = m.group(1).strip().strip("@").strip(".-")
            if " " in handle:
                handle = handle.split()[0]
            if len(handle) < 3 or handle.lower() in _AUTHOR_FP:
                continue
            if near_licence(m.start()):
                continue
            out.append(Hit("kit_author", handle.lower(), m.start(),
                           f"podpis autora „{handle}” (offset {m.start()})"))
    for m in _ICQ.finditer(text):
        out.append(Hit("kit_author", "icq:" + m.group(1), m.start(), f"ICQ {m.group(1)} (offset {m.start()})"))
    for m in _SKYPE.finditer(text):
        h = m.group(1).lower()
        if h not in _AUTHOR_FP:
            out.append(Hit("kit_author", "skype:" + h, m.start(), f"Skype {h} (offset {m.start()})"))
    return _dedup(out)


def find_panel_paths(text: str) -> list[Hit]:
    out: list[Hit] = []
    for m in _PANEL_PATH.finditer(text):
        out.append(Hit("kit_panel_path", "/" + m.group(1).lower(), m.start(),
                       f"ścieżka panelu {m.group(1)} (offset {m.start()})"))
    return _dedup(out)


def _dedup(hits: list[Hit]) -> list[Hit]:
    out, seen = [], set()
    for h in hits:
        k = (h.kind, h.value)
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out


# ------------------------------------------------------------------------------------------- orchestration


@dataclass
class _Source:
    text: str
    label: str              # short source description for the note
    artifact_id: str | None = None
    har_entry: int | None = None
    dom_path: str | None = None
    kit_source: bool = False  # PHP/other server source: mail drops only here


def _scan_source(sink: GeneSink, src: _Source) -> None:
    text = src.text[:MAX_TEXT_SCAN]
    telegram_ctx = bool(_TG_HOST.search(text))
    hits: list[Hit] = []
    hits += find_telegram(text, telegram_context=telegram_ctx)
    hits += find_discord(text)
    hits += find_sitekeys(text)
    hits += find_authors(text)
    hits += find_panel_paths(text)
    hits += find_account_widgets(text)
    if src.kit_source:
        hits += find_mail_drops(text)

    # Cheap obfuscation heuristics: rescan a concatenation-joined variant and one base64 decode layer.
    joined = _CONCAT.sub("", text) if _CONCAT.search(text) else None
    if joined and joined != text:
        for h in find_telegram(joined, telegram_context=telegram_ctx) + find_discord(joined):
            h.note += " [połączone literały JS]"
            hits.append(h)
    hits += _scan_base64(text)

    for h in _dedup(hits):
        group, quality = KIT_KINDS[h.kind]
        if h.boilerplate:
            group, quality = "boilerplate", 0.0
        sink.add(h.kind, h.value, group, artifact_id=src.artifact_id, har_entry=src.har_entry,
                 dom_path=src.dom_path, note=f"{src.label}: {h.note}", quality=quality)


def _scan_base64(text: str) -> list[Hit]:
    out: list[Hit] = []
    for m in _B64.finditer(text):
        blob = m.group(0)
        try:
            dec = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
        except Exception:  # noqa: BLE001 — bad base64 is just skipped
            continue
        try:
            s = dec.decode("utf-8", "strict")
        except UnicodeDecodeError:
            continue
        if not s.isprintable():
            continue
        for h in find_telegram(s, telegram_context=False) + find_discord(s):
            h.note += " [zdekodowane base64]"
            out.append(h)
    return out


@register("inf_kit", family=Family.INF, version=1)
def inf_kit(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, "inf_kit@1")
    b = bundle(inc)
    sources: list[_Source] = []

    for d in b.htmls:
        if d.error:
            continue
        if d.raw:
            sources.append(_Source(d.raw, "html", artifact_id=d.artifact_id, dom_path="html"))
        for i, code in enumerate(d.inline_scripts):
            sources.append(_Source(code, f"script[{i}]", artifact_id=d.artifact_id, dom_path=f"script[{i}]"))

    for a, data in b.assets:
        path = a.meta.path_in_incident
        if _is_lib_path(path):
            continue
        low = path.lower().split(".pobrane")[0]
        if not low.endswith((".js", ".mjs", ".html", ".htm", ".php", ".phtml", ".inc", ".txt", ".json")):
            continue
        sources.append(_Source(
            data.decode("utf-8", "replace"), path.rsplit("/", 1)[-1][:80],
            artifact_id=a.meta.artifact_id, dom_path=path[-200:],
            kit_source=low.endswith((".php", ".phtml", ".inc", ".txt")),
        ))

    fp = first_party_hosts(b)
    for h in b.hars:
        for body in h.bodies:
            url = body.url
            if _is_lib_path(urlsplit(url.split("#", 1)[0]).path):
                continue
            host = (urlsplit(url).hostname or "").lower()
            if is_boilerplate(host):
                continue
            try:
                txt = body.data.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            sources.append(_Source(txt, f"har:{body.entry}", artifact_id=h.artifact_id, har_entry=body.entry))
        # request URLs carry chat_id / discord webhooks / panel paths
        for r in h.requests:
            url = str(r.get("url", ""))
            if not url:
                continue
            sources.append(_Source(url, f"req:{r.get('entry')}", artifact_id=h.artifact_id,
                                   har_entry=r.get("entry")))
            # panel path only when the request is to a first-party host
            if (r.get("host") or "").lower() in fp:
                for hit in find_panel_paths(urlsplit(url).path):
                    grp, q = KIT_KINDS[hit.kind]
                    sink.add(hit.kind, hit.value, grp, artifact_id=h.artifact_id,
                             har_entry=r.get("entry"), note=f"req: {hit.note}", quality=q)

    for src in sources:
        _scan_source(sink, src)

    _run_rules(sink, b)
    return sink.result()


def _run_rules(sink: GeneSink, b) -> None:
    """Emit `kit_rule` genes from the optional YAML rule engine; never raises into the pipeline."""
    try:
        from hydra.genome import kit_rules
    except Exception:  # noqa: BLE001 — engine optional
        return
    try:
        kit_rules.emit_rule_genes(sink, b)
    except Exception:  # noqa: BLE001 — a broken engine must not stop gene extraction
        return
