"""Indicator normalisation for WATCH-RT: one canonical (kind, value) key per observable, shared by feed items and
watchlist entries, so matching is an exact dictionary lookup.

Kinds: host (full host name), domain (registrable domain), url (scheme://host/path?query, fragment dropped), ip,
phone (E.164-like digits), iban (salted hash, same as the operator-id genes), wallet, tracker, ad_id, page_id,
keyword (CT search term only, never matched against items directly).
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

KINDS = ("host", "domain", "url", "ip", "phone", "iban", "wallet", "tracker", "ad_id", "page_id", "keyword")
KIND_PL = {
    "host": "host", "domain": "domena", "url": "URL", "ip": "adres IP", "phone": "telefon", "iban": "IBAN",
    "wallet": "portfel krypto", "tracker": "identyfikator trackera", "ad_id": "identyfikator reklamy",
    "page_id": "identyfikator strony", "keyword": "słowo kluczowe (CT)",
}
# A hit on one of these kinds is a strong match: the value identifies one operator asset.
STRONG_KINDS = frozenset({"host", "url", "phone", "iban", "wallet", "tracker", "ad_id", "page_id"})

_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{2,63}$")
_PHONE_RE = re.compile(r"(?<![\w+])\+?\d[\d \-().]{6,20}\d(?!\w)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_WALLET_RES = (
    re.compile(r"\b(?:bc1|tb1)[02-9ac-hj-np-z]{11,71}\b"),       # bech32
    re.compile(r"\b[13][1-9A-HJ-NP-Za-km-z]{25,34}\b"),          # BTC legacy
    re.compile(r"\b0x[0-9a-fA-F]{40}\b"),                        # ETH
    re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b"),                # TRX
)
_TRACKER_RES = (
    re.compile(r"\b(G-[A-Z0-9]{6,12}|UA-\d{4,10}-\d{1,4}|GTM-[A-Z0-9]{4,9}|AW-\d{6,12})\b"),
)


def norm_host(value: str | None) -> str | None:
    if not value:
        return None
    h = str(value).strip().lower().rstrip(".")
    if "://" in h:
        try:
            h = (urlsplit(h).hostname or "").rstrip(".")
        except ValueError:
            return None
    h = h.split("/")[0].split(":")[0] if not h.startswith("[") else h
    h = h.removeprefix("*.").replace("[.]", ".")
    try:
        h = h.encode("idna").decode("ascii") if any(ord(c) > 127 for c in h) else h
    except UnicodeError:
        return None
    if is_ip(h):
        return h
    return h if _HOST_RE.match(h) else None


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def norm_url(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().replace("hxxp", "http").replace("[.]", ".")
    try:
        sp = urlsplit(v)
    except ValueError:
        return None
    if sp.scheme.lower() not in ("http", "https") or not sp.hostname:
        return None
    host = norm_host(sp.hostname)
    if not host:
        return None
    port = f":{sp.port}" if sp.port and sp.port not in (80, 443) else ""
    return urlunsplit((sp.scheme.lower(), host + port, sp.path or "/", sp.query, ""))


def registrable(host: str) -> str | None:
    if is_ip(host):
        return None
    try:
        from hydra.enrich.domains import registrable_domain

        reg = registrable_domain(host)
    except Exception:  # noqa: BLE001 — PSL unavailable
        reg = None
    if reg:
        return reg
    parts = host.split(".")  # suffix unknown to the PSL (e.g. reserved .example / .test): last two labels
    return ".".join(parts[-2:]) if len(parts) >= 2 else None


_HASH_RE = re.compile(r"h:[0-9a-f]{8,64}")


def norm_phone(value: str | None) -> str | None:
    """E.164-like digits; a salted gene hash (`h:<hex>`, inf_dom phones) stays a hash key, never digit-extracted."""
    if not value:
        return None
    if str(value).strip().lower().startswith("h:"):
        h = str(value).strip().lower()
        return h if _HASH_RE.fullmatch(h) else None
    digits = re.sub(r"\D", "", str(value))
    if str(value).strip().startswith("00"):
        digits = digits[2:]
    if len(digits) == 9:  # Polish national number without country code
        digits = "48" + digits
    return "+" + digits if 9 <= len(digits) <= 15 else None


def iban_key(value: str | None) -> str | None:
    """IBANs are never stored in clear: the key is the same salted hash the operator-id genes use."""
    if not value:
        return None
    v = re.sub(r"\s", "", str(value))
    if v.lower().startswith("h:"):  # already the gene hash (genes store IBANs hashed only)
        return v.lower() if _HASH_RE.fullmatch(v.lower()) else None
    v = v.upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", v):
        return None
    from hydra.genome.webparse import pii_hash

    return pii_hash(v)


def normalise(kind: str, value: str | None) -> str | None:
    """Canonical key of one indicator, or None when the value is not valid for the kind."""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if kind == "host":
        h = norm_host(v)
        return h if h and not is_ip(h) else None
    if kind == "domain":
        h = norm_host(v)
        return (registrable(h) or h) if h and not is_ip(h) else None
    if kind == "url":
        return norm_url(v)
    if kind == "ip":
        h = v.strip("[]")
        return str(ipaddress.ip_address(h)) if is_ip(h) else None
    if kind == "phone":
        return norm_phone(v)
    if kind == "iban":
        return iban_key(v)
    if kind == "wallet":
        return v if 20 <= len(v) <= 110 and re.fullmatch(r"[0-9A-Za-z]+", v) else None
    if kind == "tracker":
        return v.upper() if re.fullmatch(r"[A-Za-z0-9_\-:]{4,64}", v) else None
    if kind in ("ad_id", "page_id"):
        return v if re.fullmatch(r"[0-9A-Za-z_\-:]{3,64}", v) else None
    if kind == "keyword":
        k = v.lower()
        return k if 3 <= len(k) <= 64 and re.fullmatch(r"[a-z0-9\-]+", k) else None
    return None


def display(kind: str, value: str) -> str:
    """UI form: hosts and URLs defanged (never clickable), hashes masked."""
    if kind == "iban":
        return f"IBAN (skrót {value[:10]}…)" if value.startswith("h:") else "IBAN (ukryty)"
    if kind == "phone" and value.startswith("h:"):
        return f"telefon (skrót {value[:10]}…)"
    if kind in ("host", "domain", "url", "ip"):
        from hydra.watch.names import defang

        return defang(value)
    return value


def from_host(host: str | None) -> list[tuple[str, str]]:
    h = norm_host(host)
    if not h:
        return []
    if is_ip(h):
        return [("ip", h)]
    out = [("host", h)]
    reg = registrable(h)
    if reg:
        out.append(("domain", reg))
    return out


def from_url(url: str | None) -> list[tuple[str, str]]:
    u = norm_url(url)
    if not u:
        return []
    return [("url", u), *from_host(urlsplit(u).hostname)]


def from_text(text: str | None) -> list[tuple[str, str]]:
    """Operator identifiers in free feed text (ad bodies, URLhaus tags): phones, IBANs, wallets, trackers."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    for m in _IBAN_RE.finditer(text):
        k = iban_key(m.group(0))
        if k:
            out.append(("iban", k))
    for rx in _WALLET_RES:
        out.extend(("wallet", m.group(0)) for m in rx.finditer(text))
    for rx in _TRACKER_RES:
        out.extend(("tracker", m.group(1).upper()) for m in rx.finditer(text))
    for m in _PHONE_RE.finditer(text):
        p = norm_phone(m.group(0))
        if p and len(re.sub(r"\D", "", m.group(0))) >= 9:
            out.append(("phone", p))
    return out


def dedup(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for p in pairs:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
