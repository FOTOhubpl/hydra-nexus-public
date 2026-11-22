"""Host-name primitives for HYDRA Watch: folding, tokens, edit distance, entropy, family templates, defang.

Independent implementation (behaviour studied only through docs/cleanroom specs; no third-party scorer code).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from hydra.enrich.domains import split_host
from hydra.watch.lexicon import NOISE_LABELS

_POLISH = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "ø": "o", "ß": "ss"})
_ALNUM = re.compile(r"[a-z]+|[0-9]+")
_DIGITS = re.compile(r"[0-9]+")


@lru_cache(maxsize=4096)
def _confusable_latin(ch: str) -> str | None:
    """Latin look-alike of one non-ASCII character (Unicode confusables table via confusable_homoglyphs, MIT)."""
    try:
        from confusable_homoglyphs import confusables
    except ImportError:  # pragma: no cover - dependency is in the lock
        return None
    hits = confusables.is_confusable(ch, preferred_aliases=["latin"], greedy=True) or []
    for h in hits:
        for g in h.get("homoglyphs") or []:
            c = str(g.get("c", ""))
            if c.isascii() and c.isalnum():
                return c.lower()
    return None


def decode_idna(host: str) -> str:
    out = []
    for lab in host.split("."):
        if lab.startswith("xn--"):
            try:
                lab = lab.encode("ascii").decode("idna")
            except (UnicodeError, ValueError):
                pass
        out.append(lab)
    return ".".join(out)


def fold(text: str) -> tuple[str, bool]:
    """ASCII skeleton of a (possibly IDN) name and whether a homoglyph (non-diacritic confusable) was folded.

    Polish diacritics fold silently (ą→a, ł→l); other scripts' look-alikes (Cyrillic а → a) set the flag."""
    homoglyph = False
    out = []
    for ch in text.lower().translate(_POLISH):
        if ch.isascii():
            out.append(ch)
            continue
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        if base and base.isascii():
            out.append(base.lower())
            continue
        lat = _confusable_latin(ch)
        if lat:
            homoglyph = True
            out.append(lat)
        else:
            out.append("?")
    return "".join(out), homoglyph


@dataclass
class HostParts:
    host: str              # normalised host as seen on the wire (lower case, punycode kept)
    folded: str            # folded ASCII skeleton of the unicode form (for matching only)
    homoglyph: bool
    idn: bool
    suffix: str            # public suffix (PSL incl. private, e.g. 'pages.dev', 'com.pl'), of the real host
    domain: str            # registrable label of the real host
    sub_labels: list[str]  # subdomain labels of the real host, noise labels removed
    registrable: str       # domain + '.' + suffix (real host: officiality and families use this)
    f_domain: str = ""     # folded registrable label (brand/typo matching)
    f_sub_labels: list[str] | None = None

    @property
    def tld(self) -> str:
        return self.suffix.rsplit(".", 1)[-1] if self.suffix else ""

    @property
    def labels(self) -> list[str]:
        return [*self.sub_labels, self.domain]

    @property
    def f_labels(self) -> list[str]:
        return [*(self.f_sub_labels or []), self.f_domain]


def normalise_host(raw: str) -> str:
    h = (raw or "").strip().lower().rstrip(".")
    h = re.sub(r"^[a-z][a-z0-9+.-]*://", "", h)
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    h = h.rsplit("@", 1)[-1]
    if h.startswith("[") or re.fullmatch(r"[0-9.:]+", h):
        return h
    h = h.split(":", 1)[0]
    if not h.isascii():
        try:
            h = h.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    return h.removeprefix("*.")


def parts(host: str) -> HostParts:
    host = normalise_host(host)
    e = split_host(host)
    subs = [x for x in e.subdomain.split(".") if x] if e.subdomain else []
    while subs and subs[0] in NOISE_LABELS:
        subs = subs[1:]
    domain, suffix = e.domain or "", e.suffix or ""
    homo_any = False
    f_subs = []
    for lab in subs:
        f, h = fold(decode_idna(lab))
        f_subs.append(f)
        homo_any |= h
    f_dom, h = fold(decode_idna(domain)) if domain else ("", False)
    homo_any |= h
    return HostParts(host=host, folded=".".join([*f_subs, f_dom, suffix]).strip("."), homoglyph=homo_any,
                     idn="xn--" in host, suffix=suffix, domain=domain, sub_labels=subs,
                     registrable=f"{domain}.{suffix}" if domain and suffix else host,
                     f_domain=f_dom, f_sub_labels=f_subs)


def tokens(label: str) -> list[str]:
    """'pkobp-weryfikacja2' -> ['pkobp', 'weryfikacja', '2']."""
    return _ALNUM.findall(label.lower())


def alpha_tokens(label: str) -> list[str]:
    return [t for t in tokens(label) if t.isalpha()]


def osa_distance(a: str, b: str, limit: int = 2) -> int:
    """Optimal-string-alignment (Damerau) distance with an early exit: returns limit+1 when above `limit`."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            best = min(best, v)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb] if prev[lb] <= limit else limit + 1


def shannon_bits(s: str) -> float:
    """Shannon entropy in bits per character."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


_SHORT = re.compile(r"(?:(?<=^)|(?<=-))[a-z]{1,2}(?=-)")


def template(label: str) -> str:
    """Digit runs become '#', a 1-2 letter hyphen-separated prefix token becomes '?' (only in generated names,
    i.e. when a digit run is present): 'innpozycja543412' -> 'innpozycja#', 'pk-offertanr92792' ->
    '?-offertanr#', 'dk-fk12gc' -> '?-fk#gc'."""
    t = _DIGITS.sub("#", label.lower())
    return _SHORT.sub("?", t) if "#" in t else t


def family_key(p: HostParts) -> str:
    """Name-pattern family of a host: digit template of the registrable label + suffix ('oferta-#.sbs').

    Hosts on shared platforms (PSL private suffixes such as pages.dev) keep their own registrable label, so two
    unrelated tenants of one platform never merge."""
    label = p.f_domain or p.domain
    return f"{template(label)}.{p.suffix}" if label else p.host


def defang(text: str) -> str:
    """Non-clickable form for the UI: 'https://a.b/c' -> 'hxxps://a[.]b/c'."""
    t = re.sub(r"^http", "hxxp", text, flags=re.I)
    if "://" in t:
        scheme, rest = t.split("://", 1)
        host, slash, path = rest.partition("/")
        return f"{scheme}://{host.replace('.', '[.]')}{slash}{path}"
    return t.replace(".", "[.]")
