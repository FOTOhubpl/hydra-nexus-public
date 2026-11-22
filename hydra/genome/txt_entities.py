"""Regex / gazetteer entity extraction on light-normalised text (TXT family).

Every finder returns `Entity(kind, value, start, end, quality)` with offsets into the input string, so
the caller can build exact evidence pointers. Values are canonical (phones E.164, prices "<amount> PLN",
hosts lowercase without "www.").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hydra.genome import adv_f01
from hydra.genome.lexicons import (
    FIRST_NAMES,
    INSTITUTION_BRANDS,
    KNOWN_PRODUCTS,
    MEDIA_BRANDS,
    PERSONS,
    PRODUCT_CONTEXT,
    compile_terms,
    local,
)
from hydra.genome.txt_norm import fold


@dataclass(frozen=True)
class Entity:
    kind: str
    value: str
    start: int
    end: int
    quality: float = 1.0
    note: str | None = None
    tier: str | None = None   # overrides the kind's default tier (operator_ids: phone tiering, bank code)
    group: str | None = None  # dependency-group name hint (e.g. "contact", "messenger")


# ------------------------------------------------------------------------------ OCR post-fix

_FOREIGN = str.maketrans({
    "Ž": "Ż", "ž": "ż", "Ô": "Ó", "ô": "ó", "Ò": "Ó", "ò": "ó", "Ö": "Ó", "ö": "ó", "Õ": "Ó", "õ": "ó",
    "Ő": "Ó", "ő": "ó", "Á": "A", "á": "a", "À": "A", "à": "a", "Â": "A", "â": "a", "Ä": "A", "ä": "a",
    "Ã": "A", "ã": "a", "É": "E", "é": "e", "È": "E", "è": "e", "Ê": "Ę", "ê": "ę", "Ë": "E", "ë": "e",
    "Č": "C", "č": "c", "Š": "Ś", "š": "ś", "Ř": "R", "ř": "r", "Ň": "Ń", "ň": "ń", "Í": "I", "í": "i",
    "Ú": "U", "ú": "u", "Ù": "U", "ù": "u", "Û": "U", "û": "u", "Ü": "U", "ü": "u", "Ý": "Y", "ý": "y",
    "Ď": "D", "ď": "d", "Ť": "T", "ť": "t", "Ľ": "L", "ľ": "l", "Ĺ": "L", "ĺ": "l",
})
_PL_LOWER_DIA = set("ąćęłńóśźż")


def _upper_word_fix(m: re.Match) -> str:
    w = m.group(0)
    letters = [c for c in w if c.isalpha()]
    ups = sum(c.isupper() for c in letters)
    lows = [c for c in letters if c.islower()]
    if ups >= 2 and ups > len(lows) and lows and all(c in _PL_LOWER_DIA for c in lows):
        return w.upper()
    return w


def ocr_fix(text: str) -> str:
    """Repair systematic RapidOCR errors on Polish text (measured on the NASK sample, visual analyst notes):
    foreign accents (Ž Ô Á) -> nearest Polish letter, lowercase diacritics inside uppercase words
    ("PRZEWLEKłE"), "zt" for "zł" after amounts, letter O between digits ("2O26")."""
    t = text.translate(_FOREIGN)
    t = re.sub(r"[^\W\d_]{3,}", _upper_word_fix, t)
    t = re.sub(r"(\d)\s?zt\b", r"\1 zł", t)
    t = re.sub(r"(\d)\s?ZT\b", r"\1 ZŁ", t)
    t = re.sub(r"(?<=\d)[Oo](?=\d)", "0", t)
    return t


# ------------------------------------------------------------------------------ phones

_PHONE = re.compile(
    r"(?<![\w+])(?:(?:\+|00)\s?48[\s.-]?)?"
    r"(?:\(?\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}|\d{3}[\s.-]?\d{3}[\s.-]?\d{3})(?![\w])"
)
_PL_MOBILE_PREFIX = ("45", "50", "51", "53", "57", "60", "66", "69", "72", "73", "78", "79", "88")


_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def _inside_dotted_number(text: str, start: int, end: int) -> bool:
    """True when the match is part of an IPv4 address or of a longer dotted digit run (IP fragment, version):
    OCR of "IP 51.164.145.168" must not become the phone +48 164 145 168 (operator report 2026-11-21)."""
    for ip in _IPV4.finditer(text, max(0, start - 16), min(len(text), end + 16)):
        if ip.start() < end and start < ip.end():
            return True
    before, after = text[max(0, start - 2):start], text[end:end + 2]
    return (len(before) == 2 and before[1] == "." and before[0].isdigit()) or (
        len(after) == 2 and after[0] == "." and after[1].isdigit())


def find_phones(text: str) -> list[Entity]:
    out = []
    fspans = adv_f01.foreign_spans(text) if adv_f01.enabled() else None
    for m in _PHONE.finditer(text):
        raw = m.group(0)
        if "." in raw and _inside_dotted_number(text, m.start(), m.end()):
            continue
        if fspans is not None and adv_f01.foreign_phone(text, m.start(), m.end(), fspans):
            continue  # ADV-F01 (CR-F01-1): national part of a +CC number, not a Polish number
        digits = re.sub(r"\D", "", raw)
        has_cc = raw.lstrip().startswith(("+", "00"))
        if has_cc:
            digits = digits[2:] if digits.startswith("48") else digits[4:] if digits.startswith("0048") else digits
        if len(digits) != 9 or digits[0] in "09":
            continue  # Polish national numbers never start with 0 or 9
        nearby = text[max(0, m.start() - 25): m.start()].lower()
        phone_ctx = bool(re.search(r"(tel|telefon|zadzwo|kontakt|infolinia|whatsapp|sms|nr\.?)\W*$", nearby))
        if (not has_cc and not phone_ctx and re.fullmatch(r"\d{3}\.\d{3}\.\d{3}", raw.strip())
                and digits[:2] not in _PL_MOBILE_PREFIX):
            continue  # dotted 3-3-3 grouping of a non-mobile number is an IP/id fragment, not a phone
        # a bare 9-digit run without separators and without +48 is usually an id / account fragment
        if not has_cc and re.fullmatch(r"\d{9}", raw):
            if not phone_ctx:
                continue
            q = 0.5
        else:
            q = 0.9 if has_cc or phone_ctx else 0.7
        if digits.startswith(_PL_MOBILE_PREFIX):
            q = min(1.0, q + 0.1)
        if re.search(r"(zł|pln|€|\$|%)", text[m.end(): m.end() + 5].lower()) or "nr" in nearby and "konta" in nearby:
            continue
        note = "placeholder-like (555 / repeated digits)" if "555" in digits[2:6] or len(set(digits[3:])) <= 2 \
            else None
        out.append(Entity("entity_phone", "+48" + digits, m.start(), m.end(), round(q, 2), note))
    return out


# ------------------------------------------------------------------------------ IBAN / email / URL

_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[0-9A-Z]{4}){3,7}(?:[ ]?[0-9A-Z]{1,4})?)\b")
_NRB = re.compile(r"(?<!\d)(\d{2}(?:[ ]?\d{4}){6})(?!\d)")


def _iban_ok(iban: str) -> bool:
    s = iban[4:] + iban[:4]
    try:
        return int("".join(str(int(c, 36)) for c in s)) % 97 == 1
    except ValueError:
        return False


def find_ibans(text: str) -> list[Entity]:
    out = []
    for m in _IBAN.finditer(text):
        v = m.group(1).replace(" ", "")
        if 15 <= len(v) <= 34 and any(c.isdigit() for c in v[4:]):
            out.append(Entity("entity_iban", v, m.start(1), m.end(1), 1.0 if _iban_ok(v) else 0.5,
                              None if _iban_ok(v) else "checksum fails"))
    for m in _NRB.finditer(text):  # Polish NRB without country code
        v = "PL" + m.group(1).replace(" ", "")
        if not any(e.start <= m.start(1) < e.end for e in out):
            out.append(Entity("entity_iban", v, m.start(1), m.end(1), 1.0 if _iban_ok(v) else 0.4,
                              "NRB without PL prefix" + ("" if _iban_ok(v) else ", checksum fails")))
    return out


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zA-Z]{2,24}\b")
TLDS = frozenset("""
pl com net org info biz eu io co me tv cc app dev xyz top icu live life pro online site store shop club
news today world space website tech fun cloud link click buzz cyou vip win bid loan work agency digital
media press blog page de uk fr it es nl be at ch cz sk ua ru by lt lv ee hu ro bg hr si rs se no dk fi
pt gr tr us ca au in br mx ar cn jp kr asia mobi name gov edu int ai gg ly to ws su sbs best fit group
finance capital money market trade invest fund bank health care help email network systems solutions
""".split())
_HOST = re.compile(
    r"(?<![\w@.-])((?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,24})\.?)"
    r"(?::\d{2,5})?(/[^\s<>\"'|]*)?",
    re.I,
)


# ------------------------------------------------------------------------------ defanged IOCs

# Pasted threat reports write indicators "defanged" (hxxps://evil[.]example, user[at]mail[.]example) so they are not
# clickable. `refang` restores them before host / URL / e-mail extraction; `OffsetMap` maps match offsets in the
# refanged text back to the original text, so evidence pointers still cover the defanged span the analyst sees.
_DEFANG = re.compile(
    r"\bh[xX]{2}p(?P<s>[sS]?)(?=\[?:)"                                   # hxxp / hxxps / hXXps
    r"|(?P<sep>\[\s*:\s*/\s*/\s*\]|\[\s*:\s*\]|\(\s*:\s*\))"                # [://] [:] (:)
    r"|\s?(?P<dot>\[\s*(?:\.|dot)\s*\]|\(\s*(?:\.|dot)\s*\)|\{\s*(?:\.|dot)\s*\})\s?"  # [.] (.) {.} [dot]
    r"|\s?(?P<at>\[\s*(?:at|@)\s*\]|\(\s*(?:at|@)\s*\)|\{\s*(?:at|@)\s*\})\s?",     # [at] [@] (at)
    re.I,
)
OffsetMap = tuple[list[int], list[int]] | None


def _refang_rep(m: re.Match) -> str:
    if m.group("sep"):
        return "://" if "/" in m.group("sep") else ":"
    if m.group("dot"):
        return "."
    if m.group("at"):
        return "@"
    return "https" if m.group("s") else "http"


def refang(text: str) -> tuple[str, OffsetMap]:
    """(refanged text, offset map or None when nothing was defanged)."""
    if not _DEFANG.search(text):
        return text, None
    out: list[str] = []
    smap: list[int] = []  # output char -> original start offset
    emap: list[int] = [0]  # output prefix length -> original end offset
    pos = 0
    for m in _DEFANG.finditer(text):
        for i in range(pos, m.start()):
            out.append(text[i])
            smap.append(i)
            emap.append(i + 1)
        for c in _refang_rep(m):
            out.append(c)
            smap.append(m.start())
            emap.append(m.end())
        pos = m.end()
    for i in range(pos, len(text)):
        out.append(text[i])
        smap.append(i)
        emap.append(i + 1)
    return "".join(out), (smap, emap)


def orig_span(om: OffsetMap, start: int, end: int) -> tuple[int, int]:
    """Map a [start, end) span of the refanged text to the original text."""
    if om is None:
        return start, end
    smap, emap = om
    return (smap[start] if start < len(smap) else emap[-1]), emap[min(end, len(emap) - 1)]


def find_emails(text: str) -> list[Entity]:
    t, om = refang(text)
    return [Entity("entity_email", m.group(0).lower(), *orig_span(om, m.start(), m.end()), 1.0)
            for m in _EMAIL.finditer(t)]


def find_hosts(text: str) -> list[Entity]:
    """Domains / URLs written in text (ad copy, address bar, page body). Handed to INF as domain_in_text.
    Defanged indicators (hxxps://, [.], [:], [at]) are refanged first; offsets point into the original text."""
    out = []
    text, om = refang(text)
    emails = [(m.start(), m.end()) for m in _EMAIL.finditer(text)]
    for m in _HOST.finditer(text):
        if any(s <= m.start() < e for s, e in emails):
            continue
        host = m.group(1).lower().rstrip(".")
        tld = m.group(2).lower()
        scheme = host.startswith(("http://", "https://"))
        host = re.sub(r"^https?://", "", host)
        if tld not in TLDS:
            continue
        labels = host.split(".")
        if len(labels) < 2 or (len(labels[-2]) < 2 and not scheme and not (
                adv_f01.enabled() and host.removeprefix("www.") in adv_f01.SHORT_LINK_HOSTS)):
            continue
        # "Fot. Policja" is not a host; require lowercase or scheme or www for single-word capitalised
        raw_label = m.group(1)
        if not scheme and raw_label[:1].isupper() and raw_label[1:].islower() and "." in raw_label and \
                len(labels) == 2 and labels[0] in ("fot", "oprac", "red", "tel", "ul", "godz", "dr", "np", "itd"):
            continue
        host = host.removeprefix("www.")
        q = 1.0 if scheme or len(labels) > 2 or tld in ("pl", "com", "org", "net") else 0.8
        out.append(Entity("domain_in_text", host, *orig_span(om, m.start(), m.end()), q))
        if scheme or m.group(3):
            url = m.group(0).rstrip(".,;:)]")
            out.append(Entity("url_in_text", url if scheme else "https://" + url,
                              *orig_span(om, m.start(), m.start() + len(url)), q))
    return out


# ------------------------------------------------------------------------------ prices / amounts

_AMOUNT = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[  .]\d{3})+|\d+)(?:[.,](\d{1,2}))?\s?"
    r"(zł|zl|pln|złotych|zlotych|€|eur|euro|\$|usd|dolar\w*)(?!\w)",
    re.I,
)
_AMOUNT_PREFIX = re.compile(r"(\$|€)\s?(\d{1,3}(?:[ ,.]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)")
_CUR = {"zł": "PLN", "zl": "PLN", "pln": "PLN", "złotych": "PLN", "zlotych": "PLN", "€": "EUR", "eur": "EUR",
        "euro": "EUR", "$": "USD", "usd": "USD"}
_DISCOUNT = re.compile(r"(?<![\w])[-−–]\s?(\d{1,2})\s?%|(\d{1,2})\s?%\s?(?:zniżk|taniej|rabat|off)", re.I)
_PROMO = re.compile(r"\b(?:kod(?:em)?(?: rabatowy| promocyjny)?|promo ?code|kupon)\s*[:\-]?\s*([A-Z0-9]{4,16})\b",
                    re.I)


def _amount_value(intpart: str, frac: str | None) -> str:
    n = re.sub(r"[  .]", "", intpart)
    v = f"{int(n)}" if n.isdigit() else n
    if frac and int(frac) != 0:
        v += f".{frac.ljust(2, '0')}"
    return v


def find_amounts(text: str) -> list[Entity]:
    out = []
    for m in _AMOUNT.finditer(text):
        cur = m.group(3).lower()
        cur = _CUR.get(cur, "USD" if cur.startswith("dolar") else cur.upper())
        out.append(Entity("entity_price", f"{_amount_value(m.group(1), m.group(2))} {cur}", m.start(), m.end()))
    for m in _AMOUNT_PREFIX.finditer(text):
        if any(e.start <= m.start() < e.end for e in out):
            continue
        cur = _CUR[m.group(1)]
        out.append(Entity("entity_price", f"{_amount_value(m.group(2).replace(',', ' '), m.group(3))} {cur}",
                          m.start(), m.end()))
    for m in _DISCOUNT.finditer(text):
        out.append(Entity("entity_discount", f"{m.group(1) or m.group(2)}%", m.start(), m.end(), 0.9))
    for m in _PROMO.finditer(text):
        code = m.group(1)
        if any(c.isdigit() for c in code) or code.isupper():
            out.append(Entity("promo_code", code.upper(), m.start(1), m.end(1), 0.8))
    return out


# ------------------------------------------------------------------------------ persons

_CAP = r"[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźżéöü]+(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)?"
_CAP_PAIR = re.compile(rf"(?=\b({_CAP})\s+({_CAP})\b)")  # lookahead: overlapping pairs
_NOT_SURNAME = frozenset(fold(w) for w in """
Polska Polski Polsce Warszawa Warszawie Jerozolimskie Jerozolimskich Fot Redakcja Gazeta TVN Policja Bank
Szpital Kliniczny Platforma Program Jak Czy Kiedy Dlaczego Nie Tak Dziś Wczoraj Jutro Poniedziałek Wtorek
Środa Czwartek Piątek Sobota Niedziela Fakty Faktach Kropka Studio Sport Moto Next Magazyn
""".split()) | frozenset(fold(w) for w in (local.load("names") or {}).get("not_surname", []))


_STEM_COUNT: dict[str, int] = {}
for _p in PERSONS:
    for _st in _p.surname_stems:
        _STEM_COUNT[_st] = _STEM_COUNT.get(_st, 0) + 1
_SHARED_STEMS = {k for k, v in _STEM_COUNT.items() if v > 1}
_ALL_STEMS = tuple(k for k in _STEM_COUNT if len(k) >= 4)
COMMON_SURNAME_STEMS = tuple(fold(w) for w in """
nowak kowalsk wisniewsk wojcik kowalczyk kaminsk lewandowsk zielinsk szymansk wozniak dabrowsk kozlowsk
jankowsk mazur wojciechowsk kwiatkowsk krawczyk kaczmarek piotrowsk grabowsk zajac pawlowsk michalsk krol
wieczorek jablonsk wrobel nowakowsk majewsk olszewsk stepien malinowsk jaworsk adamczyk dudek nowick
pawlak gorsk witkowsk walczak sikora baran rutkowsk michalak szewczyk ostrowsk tomaszewsk pietrzak
""".split())


def tokens_of(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def find_persons(text: str) -> list[Entity]:
    """Gazetteer (inflection-aware) + capitalised 'Given Surname' heuristic (quality 0.5)."""
    out: list[Entity] = []
    f = fold(text)
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[a-z0-9]+", f)]
    taken: set[int] = set()
    for i, (tok, s, e) in enumerate(toks):
        for p in PERSONS:
            hit = any(tok.startswith(st) and len(tok) - len(st) <= 3 for st in p.surname_stems if len(st) >= 4) \
                or tok in p.surname_stems and len(tok) < 4 and text[s:s + 1].isupper() \
                or adv_f01.enabled() and any(adv_f01.surname_hit(tok, st) for st in p.surname_stems if len(st) >= 4)
            if not hit:
                continue
            # surname tokens must be capitalised in the original text (avoid common nouns: "lis", "duda")
            if not text[s:s + 1].isupper():
                continue
            prev = toks[i - 1][0] if i > 0 else ""
            given = bool(p.given_stems) and any(prev.startswith(g) for g in p.given_stems)
            short = min(len(st) for st in p.surname_stems) < 5
            if not given and (short or _SHARED_STEMS & set(p.surname_stems)):
                continue  # "Lis", "Duda", "Musk" or a stem shared by two people alone are too ambiguous
            start = toks[i - 1][1] if given else s
            out.append(Entity("person_named", p.canonical, start, e, 0.95 if given else 0.75,
                              f"gazetteer:{p.origin}:{p.role}"))
            taken.update({i, i - 1} if given else {i})
            break
    for m in _CAP_PAIR.finditer(text):
        g, sname = fold(m.group(1)), fold(m.group(2))
        if g not in FIRST_NAMES and g.rstrip("aey") not in FIRST_NAMES and g[:-1] not in FIRST_NAMES:
            continue
        if sname in _NOT_SURNAME or len(sname) < 3 or any(sname.startswith(st) for st in _ALL_STEMS):
            continue
        generic = any(sname.startswith(c) for c in COMMON_SURNAME_STEMS)
        standalone = len(tokens_of(text)) <= 4
        end = text.find(m.group(2), m.start() + len(m.group(1))) + len(m.group(2))
        if any(e.start <= m.start() < e.end or m.start() <= e.start < end for e in out):
            continue
        if standalone or generic:
            # a bare name line (comment header, testimonial signature) or a top-50 surname: generic persona
            out.append(Entity("commenter_name", fold(f"{m.group(1)} {m.group(2)}"), m.start(), end, 0.2,
                              "heuristic:standalone or common-surname persona"))
            continue
        given, surname = adv_f01.lemma_person(m.group(1), m.group(2)) if adv_f01.enabled() else m.groups()
        out.append(Entity("person_named", f"{given} {surname}", m.start(), end, 0.5,
                          "heuristic:capitalised-given-name"))
    return out


# ------------------------------------------------------------------------------ brands / products


def find_brands(text: str) -> list[Entity]:
    f = fold(text)
    out = []
    adv = adv_f01.enabled()
    groups = [(compile_terms(MEDIA_BRANDS), "brand_mention"), (compile_terms(INSTITUTION_BRANDS), "brand_mention"),
              (compile_terms(KNOWN_PRODUCTS), "product_name")]
    if adv:  # ADV-F01 (brand gaps): public brands missing from the shipped lexicon, brand_mention only
        groups.insert(2, (adv_f01.extra_brands(), "brand_mention"))
    for compiled, kind in groups:
        for t, rx in compiled:
            for m in rx.finditer(f):
                raw = text[m.start():m.end()]
                if " " not in raw.strip() and len(raw) <= 5 and (raw.islower() or (len(raw) <= 4 and not
                                                                                   raw.isupper() and raw != "mBank")):
                    if not (adv and kind == "brand_mention" and adv_f01.title_short_ok(raw)):
                        continue  # short single-token brands must look like brands ("PAP", "ZUS", "Onet")
                out.append(Entity(kind, t.id, m.start(), m.end(), 0.9 if kind == "brand_mention" else 1.0,
                                  f"lexicon:{t.origin}"))
    return out


_PRODUCT_NAME = re.compile(
    rf"(?:{_CAP}|[A-Z]{{2,}})(?:\s+(?:{_CAP}|[A-Z]{{2,}}|[A-Z]{{1,3}}\d*|Forte|Plus|Pro|Max|RX|AI))" r"{0,2}"
)


_NAME_TRAIL = frozenset("od do na w z i dla to ze o po przez jest sa a ale lub oraz".split())
_NOT_PRODUCT = frozenset("wiadomosci informacje twoim twoja twoje swoje nasza nasze polska polski".split())


def find_products(text: str, known_persons: set[str] | None = None) -> list[Entity]:
    """Invented brand / product names: capitalised 1-3 word names after a product context word
    ('platformy Zyskomat Prime', 'aplikacji Sweet Bonanza', 'preparat Kardiovit Forte'). Quality 0.6."""
    out = []
    f = fold(text)
    for cm in PRODUCT_CONTEXT.finditer(f):
        tail = text[cm.end(): cm.end() + 60]
        # optional adjective between ("platformy inwestycyjnej X")
        adj = re.match(r"\s+[a-ząćęłńóśźż]+(?:ej|ą|a|y|i|ego|ym)?(?=\s+[A-ZĄĆĘŁŃÓŚŹŻ])", tail)
        off = adj.end() if adj else 0
        m = _PRODUCT_NAME.match(tail[off:].lstrip())
        if not m:
            continue
        lead = len(tail[off:]) - len(tail[off:].lstrip())
        name = m.group(0).strip(" .,")
        words = name.split()
        while words and fold(words[-1]).strip() in _NAME_TRAIL:
            words.pop()
        name = " ".join(words)
        fname = " ".join(fold(name).split())
        if len(fname) < 4 or not words or fname.split()[0] in _NOT_SURNAME or fname.split()[0] in _NAME_TRAIL or \
                (known_persons and name in known_persons):
            continue
        if len(words) == 1 and (len(fname) < 5 or re.search(r"(ej|ym|im|ego|ych|ich|emu|ami|ach|oim|ego)$", fname)
                                or fname in _NOT_PRODUCT):
            continue
        s = cm.end() + off + lead
        out.append(Entity("product_name", fname, s, s + len(name), 0.6 if len(words) > 1 else 0.4,
                          "heuristic:context"))
    return out
