"""ADV-F01: extraction-accuracy fixes for CR-F01-1..6 (docs/release/EXTRACTION_ACCURACY.md), behind HYDRA_ADV_F01=1.

Default OFF. With the flag off every hook in txt_entities / operator_ids / dom / har / txt returns early and the
extractors produce the same genes and the same extractor versions as before. With the flag on:

1. Foreign phones (CR-F01-1): a Polish-looking 9-digit run that directly follows a non-+48 country code
   (`+420 601 126 480`, `+380 50 123 4567`) or lies inside a `+CC`/`00CC` number that libphonenumber
   (`phonenumbers`, already a dependency) accepts as a possible number is not re-read as `+48...`.
2. Redirect chains (CR-F01-2): a 2xx HTML/JS response in a HAR whose body (meta refresh, `location=`,
   `location.replace/assign`) or `Refresh` header points to a URL the browser requested afterwards becomes a hop;
   the chain is built by following hop targets (exact URL, else same host), loop-safe (each hop used once,
   bounded length), so a chain is neither truncated at an HTML hop nor polluted by unrelated sub-resource 3xx.
3. IBAN (CR-F01-4): Unicode-aware boundaries (`... 1332 Złoty` no longer glues `Z`), and when the raw match fails
   mod-97 / the country length (schwifty IBAN registry), the longest prefix ending at a group boundary that is a
   complete valid IBAN is used; OCR letter/digit confusions are repaired only in positions the country's BBAN
   format declares numeric, and only when the repaired number passes mod-97 (no search over alternatives).
4. Person names (CR-F01-3): gazetteer surnames accept Polish case endings longer than 3 letters
   (-iego/-iemu/-iej/-ich/-imi); the heuristic 'Given Surname' value is lemmatised to the nominative
   (`Krzysztofa Stanowskiego` -> `Krzysztof Stanowski`).
5. Brands (CR-F01-5): four-letter title-case brand tokens (`Onet`, `Lidl`) count; a small set of public brands that
   Polish phishing impersonates and the shipped lexicon lacks is added for `brand_mention` only.
6. Telegram (CR-F01-6): a bare `t.me/<handle>` (and the other one-letter short-link hosts below) yields
   domain_in_text / url_in_text; `@abcd` four-character handles next to the word Telegram are accepted.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from urllib.parse import urldefrag, urljoin, urlsplit

FLAG = "HYDRA_ADV_F01"


def enabled() -> bool:
    # Default ON since 2026-11-22 (operator: foreign numbers such as +420 must not become +48; CR-F01-1..6).
    # HYDRA_ADV_F01=0 restores the previous extraction.
    return os.environ.get(FLAG, "1") == "1"


# Extractor versions with the flag on (cache keys change, so stored genes are re-extracted).
_ON_VERSION = {"txt_entities": 7, "txt_lines": 2, "inf_dom": 6, "inf_har": 4}


def version(name: str, off: int) -> int:
    """Version of an extractor whose output this module changes: `off` without the flag."""
    return _ON_VERSION.get(name, off) if enabled() else off


# =============================================================================================== 1. phones

_CC_BEFORE = re.compile(r"(?:\+|(?<!\d)00)\s?\(?([1-9]\d{0,2})\)?[\s.()-]{0,2}$")


def foreign_spans(text: str) -> list[tuple[int, int]]:
    """Spans of +CC / 00CC (CC != 48) numbers that libphonenumber accepts as possible numbers."""
    if "+" not in text and "00" not in text:
        return []
    import phonenumbers

    from hydra.genome.operator_ids import _INTL

    out = []
    for m in _INTL.finditer(text):
        raw = "+" + m.group(1) + re.sub(r"\D", "", m.group(2))
        try:
            num = phonenumbers.parse(raw, None)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_possible_number(num):
            out.append(m.span())
    return out


def foreign_phone(text: str, start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """True when the Polish-looking number at text[start:end] is the national part of a foreign number."""
    m = _CC_BEFORE.search(text[max(0, start - 9):start])
    if m and m.group(1) != "48":
        return True
    return any(s < end and start < e for s, e in spans)


# =============================================================================================== 2. redirects

_REFRESH_HDR = re.compile(r"^\s*\d+\s*[;,]\s*url\s*=\s*['\"]?([^'\"\s]+)", re.I)
MAX_CHAIN = 32


def _norm(u: str) -> str:
    u = urldefrag(u)[0]
    sp = urlsplit(u)
    return f"{sp.scheme.lower()}://{(sp.hostname or '').lower()}{sp.path or '/'}{'?' + sp.query if sp.query else ''}"


def body_hops(h) -> list[dict]:
    """HTML / JS / Refresh-header hops of a parsed HAR (webparse.HarParsed) that the browser followed: the target
    URL (or a document with the same host and path) is requested later in the HAR."""
    from hydra.genome.webparse import META_REFRESH, REDIR_RX

    pos = {r["entry"]: k for k, r in enumerate(h.requests)}
    by_entry = {r["entry"]: r for r in h.requests}
    have = {r["entry"] for r in h.redirects}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    last_url: dict[str, int] = {}             # normalised URL -> last request position
    last_doc: dict[tuple[str, str], int] = {}  # (host, path) of document requests -> last position
    for k, r in enumerate(h.requests):
        last_url[_norm(r["url"])] = k
        if r["type"] == "document" or r["mime"] == "text/html":
            last_doc[(r["host"], urlsplit(r["url"]).path or "/")] = k

    def later(entry: int, target: str) -> bool:
        k = pos.get(entry)
        if k is None:
            return False
        sp = urlsplit(target)
        # same URL, or same host and path with another query (click ids appended by the TDS): a document only
        return last_url.get(_norm(target), -1) > k or last_doc.get(((sp.hostname or "").lower(), sp.path or "/"),
                                                                     -1) > k

    def add(kind: str, frm: str, target: str, entry: int) -> None:
        to = urljoin(frm, target.strip().strip("'\""))
        if not urlsplit(to).scheme.startswith("http") or _norm(to) == _norm(frm):
            return
        if (frm, to) in seen or not later(entry, to):
            return
        seen.add((frm, to))
        out.append({"kind": kind, "from": frm, "to": to, "entry": entry})

    for entry, _host, hdrs in getattr(h, "doc_headers", []) or []:  # `Refresh: 0; url=...` response header
        if entry in have:
            continue
        for name, value in hdrs:
            if name.lower() == "refresh":
                m = _REFRESH_HDR.match(value)
                if m and entry in by_entry:
                    add("http_refresh", by_entry[entry]["url"], m.group(1), entry)
    for body in h.bodies:
        r = by_entry.get(body.entry)
        if r is None or body.entry in have or body.url.endswith("#post") or not (200 <= r["status"] < 300):
            continue
        if not (body.mime in ("text/html", "application/xhtml+xml") or "javascript" in body.mime):
            continue
        txt = body.data[:300_000].decode("utf-8", errors="replace")
        for m in META_REFRESH.findall(txt):
            add("meta_refresh", body.url, m, body.entry)
        for kind, rx in REDIR_RX:
            if kind == "window_open":  # a pop-up is not a navigation of the page
                continue
            for m in rx.findall(txt):
                if m.startswith(("#", "javascript", "data:")) or "+" in m:
                    continue
                add(kind, body.url, m, body.entry)
    return out


def all_hops(h, hops: list[dict]) -> list[dict]:
    """3xx hops plus followed body hops, in request order."""
    from hydra.enrich.domains import is_boilerplate

    extra = [r for r in body_hops(h) if not is_boilerplate(urlsplit(r["from"]).hostname or "")]
    if not extra:
        return hops
    pos = {r["entry"]: k for k, r in enumerate(h.requests)}
    return sorted(hops + extra, key=lambda r: (pos.get(r["entry"], 1 << 30), r["kind"].startswith("http3")))


def follow_chain(hops: list[dict]) -> list[dict]:
    """The navigation chain starting at the first hop: each next hop starts at the previous target (same URL,
    else same host). Every hop is used at most once and the chain is bounded, so redirect loops terminate."""
    if not hops:
        return []
    chain, used = [hops[0]], {0}
    while len(chain) < MAX_CHAIN:
        to = chain[-1]["to"]
        nt, th = _norm(urljoin(chain[-1]["from"], to)), (urlsplit(urljoin(chain[-1]["from"], to)).hostname or "")
        nxt = next((k for k, r in enumerate(hops) if k not in used and _norm(r["from"]) == nt), None)
        if nxt is None:
            nxt = next((k for k, r in enumerate(hops) if k not in used and k > max(used)
                        and (urlsplit(r["from"]).hostname or "").lower() == th.lower()), None)
        if nxt is None:
            break
        used.add(nxt)
        chain.append(hops[nxt])
    return chain


# =============================================================================================== 3. IBAN

IBAN_RX = re.compile(r"(?<![^\W_])([A-Z]{2}\d{2}(?:[ ]?[0-9A-Z]{4}){2,7}(?:[ ]?[0-9A-Z]{1,4})?)(?![^\W_])")
_OCR_DIGIT = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8",
                            "G": "6", "T": "7"})


@lru_cache(maxsize=256)
def _spec(cc: str):
    try:
        from schwifty import registry

        return registry.get_iban_spec(cc)
    except Exception:  # noqa: BLE001 — unknown country / schwifty missing
        return None


@lru_cache(maxsize=256)
def _numeric_positions(cc: str) -> tuple[bool, ...] | None:
    sp = _spec(cc)
    if sp is None:
        return None
    out: list[bool] = []
    for n, t in re.findall(r"(\d+)!?([nace])", sp.bban_spec):
        out += [t == "n"] * int(n)
    return tuple(out)


def _valid(v: str) -> bool:
    from hydra.genome.operator_ids import iban_mod97

    sp = _spec(v[:2])
    return iban_mod97(v) and (sp is None or (len(v) == sp.iban_length and bool(sp.regex.match(v[4:]))))


def _ocr_repair(v: str) -> str | None:
    num = _numeric_positions(v[:2])
    if num is None or len(v) != len(num) + 4:
        return None
    b = "".join(c.translate(_OCR_DIGIT) if is_n else c for c, is_n in zip(v[4:], num, strict=True))
    fixed = v[:4] + b
    return fixed if fixed != v and _valid(fixed) else None


def refine_iban(raw: str) -> tuple[str, int]:
    """(IBAN, characters of `raw` it uses). The raw match if it is valid; else the longest group-boundary
    prefix that is a complete valid IBAN of its country; else an OCR-repaired form; else the raw match."""
    v = raw.replace(" ", "")
    if _valid(v):
        return v, len(raw)
    cuts = [k for k, c in enumerate(raw) if c == " "] if " " in raw else []
    for k in reversed(cuts):
        p = raw[:k].replace(" ", "")
        if len(p) >= 15 and _valid(p):
            return p, k
    sp = _spec(v[:2])
    if sp is not None and len(v) > sp.iban_length and _valid(v[:sp.iban_length]):  # no spaces: exact length
        n = 0
        for k, c in enumerate(raw):
            n += c != " "
            if n == sp.iban_length:
                return v[:sp.iban_length], k + 1
    fixed = _ocr_repair(v)
    if fixed:
        return fixed, len(raw)
    return v, len(raw)


# =============================================================================================== 4. persons

PERSON_ENDINGS = ("iego", "iemu", "iej", "ich", "imi", "owie", "owi", "em", "ego", "emu")


def surname_hit(tok: str, stem: str) -> bool:
    """Gazetteer stem match with Polish case endings longer than the default 3-letter allowance."""
    return tok.startswith(stem) and tok[len(stem):] in PERSON_ENDINGS


_SUR_M = re.compile(r"(sk|ck|dzk)(iego|iemu|im)$")
_SUR_F = re.compile(r"(sk|ck|dzk)(iej|a)$")


def lemma_person(given: str, surname: str) -> tuple[str, str]:
    """Nominative of an inflected 'Given Surname' pair (only -ski/-cki/-dzki surnames and given names whose
    nominative is in the first-name list; anything else is returned unchanged)."""
    from hydra.genome.lexicons import FIRST_NAMES
    from hydra.genome.txt_norm import fold

    g = given
    fg = fold(given)
    if fg not in FIRST_NAMES:
        if fold(given[:-1]) in FIRST_NAMES:          # Krzysztofa -> Krzysztof, Rafała -> Rafał
            g = given[:-1]
        elif given.endswith(("y", "i")) and fold(given[:-1] + "a") in FIRST_NAMES:  # Anny -> Anna
            g = given[:-1] + "a"
        elif given.endswith(("ie", "ę")) and fold(given[:-1].rstrip("i") + "a") in FIRST_NAMES:  # Annie/Annę
            g = given[:-1].rstrip("i") + "a"
    s = surname
    if surname.lower().endswith(("skiego", "ckiego", "skiemu", "ckiemu", "skim", "ckim")):
        s = re.sub(r"(iego|iemu|im)$", "i", surname)
    elif surname.lower().endswith(("skiej", "ckiej")) or (g != given and g.endswith("a")
                                                           and surname.lower().endswith(("ską", "cką"))):
        s = re.sub(r"(iej|ą)$", "a", surname)
    return g, s


# =============================================================================================== 5. brands

def title_short_ok(raw: str) -> bool:
    """A four-letter brand token written in title case ('Onet', 'Lidl')."""
    return len(raw) == 4 and raw[:1].isupper() and raw[1:].islower()


@lru_cache(maxsize=1)
def extra_brands():
    """Public brands commonly impersonated in Polish phishing (CERT Polska / CSIRT KNF warnings, general
    knowledge: origin 'prior') that the shipped lexicon does not list. Multi-word or unambiguous patterns only;
    short ambiguous words (Play, Plus, LOT, Bolt, Ups) are deliberately absent."""
    from hydra.genome.lexicons import T, compile_terms

    return compile_terms((
        T("orange", r"orange polska", r"orange pl", r"siec orange"),
        T("t_mobile", r"t mobile", r"tmobile"),
        T("plus_gsm", r"plus gsm", r"siec plus", r"plus pl"),
        T("play", r"siec play", r"play pl"),
        T("bnp_paribas", r"bnp paribas"),
        T("citi_handlowy", r"citi handlowy", r"citibank"),
        T("velobank", r"velobank"),
        T("nest_bank", r"nest bank"),
        T("bank_pocztowy", r"bank pocztowy"),
        T("inteligo", r"inteligo"),
        T("paypal", r"paypal"),
        T("amazon", r"amazon"),
        T("fedex", r"fedex"),
        T("ups", r"ups polska"),
        T("pkp_intercity", r"pkp intercity", r"intercity"),
        T("media_expert", r"media expert"),
        T("rtv_euro_agd", r"rtv euro agd", r"euro rtv agd"),
        T("zabka", r"zabka", r"zabce"),
        T("temu", r"temu com"),
        T("shein", r"shein"),
        T("cert_polska", r"cert polska", r"cert pl"),
        T("cba", r"centraln\w* biur\w* antykorupcyjn\w*"),
        T("abw", r"agencj\w* bezpieczenstwa wewnetrznego"),
        T("epuap", r"epuap", r"profil\w* zaufan\w*"),
        T("radio_zet", r"radio zet"),
        T("puls_biznesu", r"puls biznesu"),
    ))


# ============================================================================ 6. Telegram / short hosts

SHORT_LINK_HOSTS = frozenset({"t.me", "m.me", "t.co", "x.com"})
AT_HANDLE4 = re.compile(r"(?<![\w@.])@([A-Za-z][A-Za-z0-9_]{3,31})(?![\w@.])")
