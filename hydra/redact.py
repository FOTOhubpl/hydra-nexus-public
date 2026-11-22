"""Redaction of secrets and PII found inside organiser material (FORMATS.md §23, §35).

The sample leaks an FB `access_token`, API keys in hidden form fields and URLs, and Windows user names in
`file:///C:/Users/<user>/…` paths. Anything we persist for display or export (gene values, evidence notes,
enrichment queries, titles) goes through `redact_text` / `redact_url`. Originals in the CAS stay
byte-identical; they are evidence.

This module is the one redaction path for every output boundary (CR-ASTRA-SEC-12/13/14): API previews
(artifact text/HAR/HTML), exports (XARF, STIX, Neo4j, CSV, dataset), job/problem responses, operational logs
and LLM prompts, answers and cache replays. Context-aware rules cover HTML form fields, HAR name/value
header pairs and cookies, JSON/JS key-value pairs in text, URL query values and URL userinfo passwords.

Matching on a secret remains possible through `secret_fingerprint`: a salted, truncated sha256 (the salt
is per installation, `HYDRA_REDACT_SALT`, so fingerprints are not reversible by dictionary on another box).
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from hydra.contracts import REDACTED

# Query/form keys whose values are credentials or per-user secrets. Lower-case, matched exactly or by suffix.
SECRET_KEYS = {
    "access_token", "accesstoken", "token", "auth", "authorization", "api_key", "apikey", "key", "lpkey",
    "secret", "client_secret", "sig", "signature", "password", "passwd", "pass", "pwd", "session",
    "sessionid", "sid", "jwt", "id_token", "refresh_token", "code", "upd_key", "private_key", "fbtoken",
}
SECRET_SUFFIXES = ("_token", "_secret", "_key", "_sig", "_signature", "_password", "token", "apikey", "password",
                   "passwd", "_pwd", "_cookie", "_credential", "_credentials", "_sessionid")
# HTTP header / cookie names whose values are credentials (HAR `{"name": ..., "value": ...}` pairs, `cookies`).
SECRET_HEADERS = {"cookie", "set_cookie", "authorization", "proxy_authorization", "x_api_key", "x_auth_token",
                  "x_csrf_token", "x_xsrf_token", "x_access_token", "x_amz_security_token", "credential",
                  "credentials", "passphrase", "csrf", "xsrf", "csrfmiddlewaretoken"}

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
FB_TOKEN_RE = re.compile(r"\bEAA[A-Za-z0-9]{30,}\b")              # Meta Graph API access token
GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")
BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}")
# Telegram bot token (<account id>:<30-45 url-safe chars>): keep the non-secret id, redact the token. Also
# matches inside api.telegram.org/bot<token>/ URLs. Discord webhook: keep the numeric id, redact the token.
STRIPE_SECRET_RE = re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,120}\b")  # Stripe secret / restricted keys
TELEGRAM_BOT_URL_RE = re.compile(r"(?i)(api\.telegram\.org/bot\d{8,12}):[A-Za-z0-9_-]{30,45}")
TELEGRAM_BOT_RE = re.compile(r"(?<![\w-])(\d{8,12}):[A-Za-z0-9_-]{30,45}(?![A-Za-z0-9_-])")
DISCORD_WEBHOOK_RE = re.compile(
    r"(?i)(discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d{17,20})/[A-Za-z0-9_.\-]{50,90}"
)
WIN_USER_RE = re.compile(r"(?i)([A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)([^\\/\s\"'<>?#&]+)")
NIX_USER_RE = re.compile(r"(/(?:Users|home)/)([^/\s\"'<>?#&]+)")
QUERY_SECRET_RE = re.compile(r"(?i)([?&;](?P<k>[A-Za-z0-9_.-]{1,40})=)(?P<v>[^&#\s\"'<>]+)")
# `scheme://user:password@host`: the password goes; a bare `user@host` stays (deceptive userinfo is evidence).
USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,15}://[^/\s:@?#\"'<>]{0,256}:)([^/\s@?#\"'<>]{1,512})(@)")
# HTML elements that carry a value: `<input name="password" value="…">`, `<meta name="csrf-token" content="…">`.
HTML_TAG_RE = re.compile(r"(?i)<(?:input|meta|param|textarea|option|button|select)\b[^<>]{0,4000}>")
HTML_ATTR_RE = re.compile(r"""(?i)\b(name|id|property|http-equiv|type|autocomplete)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
HTML_VALUE_RE = re.compile(r"""(?i)(\b(?:value|content|data-value)\s*=\s*)("[^"]*"|'[^']*'|[^\s>]+)""")
# `<textarea name="password">secret</textarea>` carries its value as element content, not as an attribute
# (CR-ASTRA-SW-03). The opening tag is classified like any other form field; the content up to the closing tag
# (case and whitespace tolerant) goes. An unclosed textarea is redacted up to the next tag or the end of the text.
TEXTAREA_OPEN_RE = re.compile(r"(?i)<textarea\b[^<>]{0,4000}>")
TEXTAREA_CLOSE_RE = re.compile(r"(?i)</\s*textarea\s*>")
# HAR header/cookie pairs written as text, in either key order (`{"name": "X-Api-Key", "value": "…"}`). Used when
# a document is not parsed as a whole (cut preview window, over the parse budget, broken JSON) (CR-ASTRA-SW-04).
# A value cut off by the end of the window (no closing quote) is redacted up to the end.
_JSTR = r'(?:[^"\\\n]|\\.){0,8000}'
HAR_NAME_VALUE_RE = re.compile(
    r'(?P<pre>"name"\s*:\s*"(?P<k>[^"\\\n]{1,100})"\s*,\s*"value"\s*:\s*")(?P<v>' + _JSTR + r')(?P<end>"|\Z)')
HAR_VALUE_NAME_RE = re.compile(
    r'(?P<pre>"value"\s*:\s*")(?P<v>' + _JSTR + r')(?P<end>"\s*,\s*"name"\s*:\s*"(?P<k>[^"\\\n]{1,100})")')
# JSON / JS object pairs with a quoted key, and JS assignments with a bare identifier: `"password": "…"`,
# `apiKey = '…'`. Only string values; numbers and booleans are left alone. Escaped JSON (`\"password\": \"…\"`)
# is covered by the optional backslash before each quote.
QUOTED_PAIR_RE = re.compile(
    r"""(?P<kq>\\?["'])(?P<k>[A-Za-z0-9_.\-]{1,60})(?P=kq)\s*:\s*(?P<vq>\\?["'])"""
    r"""(?P<v>(?:(?!(?P=vq))[^\n\\]|\\[^"'\n]){0,4000})(?P=vq)""")
BARE_PAIR_RE = re.compile(
    r"""(?<![\w"'.\-\\])(?P<k>[A-Za-z_][A-Za-z0-9_]{0,60})\s*[:=]\s*(?P<vq>["'])"""
    r"""(?P<v>(?:(?!(?P=vq))[^\n\\]|\\.){1,4000})(?P=vq)""")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _norm_key(key: str) -> str:
    return _CAMEL.sub("_", key.strip()).lower().replace("-", "_").replace(".", "_")


def is_secret_key(key: str) -> bool:
    """Credential-like field name: exact, by suffix, header style (`X-Api-Key`) or camelCase (`apiKey`)."""
    if not key:
        return False
    return any(c in SECRET_KEYS or c in SECRET_HEADERS or c.endswith(SECRET_SUFFIXES)
               for c in (key.lower().strip(), _norm_key(key)))


def secret_fingerprint(value: str) -> str:
    salt = os.environ.get("HYDRA_REDACT_SALT", "hydra-nexus")
    return "h:" + hashlib.sha256(f"{salt}|{value}".encode()).hexdigest()[:12]


def _sub_query(m: re.Match[str]) -> str:
    if is_secret_key(m.group("k")) and m.group("v") != REDACTED and m.group("v") != quote(REDACTED):
        return m.group(1) + REDACTED
    return m.group(0)


def _unquote_attr(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def _tag_is_secret(tag: str) -> bool:
    attrs = {a.group(1).lower(): _unquote_attr(a.group(2)) for a in HTML_ATTR_RE.finditer(tag)}
    return (attrs.get("type", "").lower() == "password"
            or attrs.get("autocomplete", "").lower() in {"current-password", "new-password", "one-time-code"}
            or any(is_secret_key(attrs.get(a, "")) for a in ("name", "id", "property", "http-equiv")))


def _sub_html_tag(m: re.Match[str]) -> str:
    tag = m.group(0)
    if not _tag_is_secret(tag):
        return tag

    def value(v: re.Match[str]) -> str:
        raw = v.group(2)
        if _unquote_attr(raw) in ("", REDACTED):
            return v.group(0)
        q = raw[0] if raw[0] in "\"'" else '"'
        return f"{v.group(1)}{q}{REDACTED}{q}"
    return HTML_VALUE_RE.sub(value, tag)


def _redact_textareas(text: str) -> str:
    """Element content of credential `<textarea>`s replaced by REDACTED (forward scan, no backtracking)."""
    out, pos = [], 0
    for m in TEXTAREA_OPEN_RE.finditer(text):
        if m.start() < pos or not _tag_is_secret(m.group(0)):
            continue
        close = TEXTAREA_CLOSE_RE.search(text, m.end())
        if close:
            end = close.start()
        else:
            nxt = text.find("<", m.end())
            end = len(text) if nxt < 0 else nxt
        body = text[m.end():end]
        out.append(text[pos:m.end()])
        out.append(body if body.strip() in ("", REDACTED) else REDACTED)
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


def _sub_har_pair(m: re.Match[str]) -> str:
    if not is_secret_key(m.group("k")) or m.group("v") in ("", REDACTED):
        return m.group(0)
    return m.group("pre") + REDACTED + m.group("end")


def _sub_pair(m: re.Match[str]) -> str:
    if not _secret_value(m.group("k"), m.group("v")):
        return m.group(0)
    base, (start, end) = m.start(0), m.span("v")
    whole = m.group(0)
    return whole[: start - base] + REDACTED + whole[end - base:]


def redact_text(text: str | None) -> str | None:
    """Redact secrets anywhere in free text: URLs (secret query values, userinfo passwords), bearer tokens,
    JWT / Meta / Google keys, local user names, credential fields in HTML (`<input name="password" value=…>`)
    and JSON/JS pairs (`"api_key": "…"`). Idempotent; text without secrets is returned unchanged."""
    if not text:
        return text
    out = JWT_RE.sub(REDACTED, text)
    out = STRIPE_SECRET_RE.sub(REDACTED, out)
    out = TELEGRAM_BOT_URL_RE.sub(lambda m: m.group(1) + ":" + REDACTED, out)
    out = TELEGRAM_BOT_RE.sub(lambda m: m.group(1) + ":" + REDACTED, out)
    out = DISCORD_WEBHOOK_RE.sub(lambda m: m.group(1) + "/" + REDACTED, out)
    out = FB_TOKEN_RE.sub(REDACTED, out)
    out = GOOGLE_KEY_RE.sub(REDACTED, out)
    out = BEARER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = WIN_USER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = NIX_USER_RE.sub(lambda m: m.group(1) + REDACTED if m.group(2) != REDACTED else m.group(0), out)
    out = QUERY_SECRET_RE.sub(_sub_query, out)
    out = USERINFO_RE.sub(lambda m: m.group(0) if m.group(2) == REDACTED else m.group(1) + REDACTED + m.group(3), out)
    if "<" in out:
        out = HTML_TAG_RE.sub(_sub_html_tag, out)
        out = _redact_textareas(out)
    if '"value"' in out:
        out = HAR_NAME_VALUE_RE.sub(_sub_har_pair, out)
        out = HAR_VALUE_NAME_RE.sub(_sub_har_pair, out)
    if ":" in out or "=" in out:
        out = QUOTED_PAIR_RE.sub(_sub_pair, out)
        out = BARE_PAIR_RE.sub(_sub_pair, out)
    return out


def redact_url(url: str | None) -> str | None:
    """URL with secret query values replaced, userinfo passwords removed and local user names removed from
    file:// paths."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_text(url)
    if parts.scheme == "file":
        return redact_text(url)
    if not parts.query:
        return redact_text(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    q = "&".join(f"{k}={REDACTED if is_secret_key(k) else v}" for k, v in pairs)
    return redact_text(urlunsplit((parts.scheme, parts.netloc, parts.path, q, parts.fragment)))


# Contract / bookkeeping fields whose names look secret-ish but hold ids, hashes or tiers (reviewer #19).
SAFE_FIELDS = {"key", "enrichment_key", "dependency_group", "vector_ref", "artifact_id", "response_sha256",
               "cache_path", "kind", "group_key", "gene_key", "sort_key", "run_id", "incident_id", "campaign_id",
               "group_id", "model", "source", "extractor"}
# Only our own salted fingerprints are exempt: a secret-named field holding raw hex is still a secret
# (CR-ASTRA-SEC-12, `{"password": "ab12ab12…"}`). Hashes live under SAFE_FIELDS names.
_FINGERPRINT = re.compile(r"^h:[0-9a-f]{8,64}$")


def _secret_value(key: str, value: str) -> bool:
    """A value under a secret-named key is redacted unless the field is a known contract field or the value is
    already one of our fingerprints."""
    if key.lower() in SAFE_FIELDS or not value or value in (REDACTED, quote(REDACTED)):
        return False
    return is_secret_key(key) and not _FINGERPRINT.match(value)


def redact_obj(obj, _parent: str = ""):
    """Recursively redact strings in JSON-like structures (dict keys are kept). Secret-named fields are replaced,
    HAR header/cookie pairs `{"name": "X-Api-Key", "value": …}` lose their value, every entry of a `cookies` list
    is replaced, free text goes through `redact_text`."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        name = obj.get("name")
        pair_secret = isinstance(name, str) and isinstance(obj.get("value"), str) and (
            is_secret_key(name) or _parent.lower() == "cookies")
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and v and v != REDACTED and (
                    _secret_value(str(k), v) or (pair_secret and k == "value")):
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v, str(k))
        return out
    if isinstance(obj, list | tuple):
        return [redact_obj(v, _parent) for v in obj]
    return obj


def redact_document(data: bytes, mime: str = "") -> bytes:
    """Redacted copy of a text document (HTML, HAR/JSON, plain text) for previews and evidence packages.
    JSON is redacted structurally (HAR headers, cookies, secret-named fields) and as text. Returns the
    original bytes when nothing changed, so clean documents stay byte-identical to the CAS original."""
    import json

    text = data.decode("utf-8", errors="replace")
    red = text
    if "json" in mime or mime.endswith("+har") or text.lstrip().startswith(("{", "[")):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if obj is not None:
            cleaned = redact_obj(obj)
            if cleaned != obj:
                red = json.dumps(cleaned, ensure_ascii=False, indent=1)
    red = redact_text(red) or ""
    return data if red == text else red.encode("utf-8")
