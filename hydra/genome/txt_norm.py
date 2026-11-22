"""Text normalisation, tokenisation, obfuscation detection and language guess (TXT family helpers).

Two levels (PLAN_MAX §1.8, lab result: aggressive normalisation costs e5 ~0.10 R@1):
- `light()`  for embeddings, display and entity extraction: ftfy, NFKC (folds fullwidth), zero-width and
  soft-hyphen removal, homoglyph folding inside mixed-script words (Cyrillic/Greek look-alikes in a Latin
  word), whitespace collapse. Pure Cyrillic words (e.g. Ukrainian form values) are left alone.
- `fold()`   for MinHash/shingles/lexicon matching: lowercase, strip diacritics (incl. ł), every
  non-alphanumeric char becomes a space. **Length-preserving** with respect to its input, so a match
  offset in `fold(s)` is a valid offset into `s` (evidence char ranges stay exact).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­᠎‎‏‪‫‬‭‮"))
_WS = re.compile(r"\s+")

# Common Cyrillic/Greek look-alikes of Latin letters (fallback when confusable_homoglyphs has no entry).
_HOMO = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ӏ": "l", "һ": "h", "ԛ": "q", "ԝ": "w", "ѵ": "v", "А": "A", "В": "B", "Е": "E", "К": "K",
    "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ј": "J",
    "Ѕ": "S", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N",
    "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "ο": "o", "ν": "v", "α": "a", "ι": "i", "κ": "k",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
}
_EXTRA_FOLD = {"ł": "l", "Ł": "l", "ø": "o", "Ø": "o", "đ": "d", "Đ": "d", "ß": "s", "æ": "a", "œ": "o", "ı": "i"}
LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}


def _script(ch: str) -> str:
    if not ch.isalpha():
        return ""
    name = unicodedata.name(ch, "")
    for s in ("LATIN", "CYRILLIC", "GREEK"):
        if name.startswith(s):
            return s
    return "OTHER"


@lru_cache(maxsize=4096)
def _latin_lookalike(ch: str) -> str | None:
    if ch in _HOMO:
        return _HOMO[ch]
    try:
        from confusable_homoglyphs import confusables

        res = confusables.is_confusable(ch, greedy=True, preferred_aliases=["latin"])
    except Exception:  # noqa: BLE001
        return None
    if not res:
        return None
    for h in res[0].get("homoglyphs", []):
        c = h.get("c", "")
        if len(c) == 1 and c.isascii() and c.isalpha():
            return c
    return None


_WORD = re.compile(r"\w+", re.UNICODE)


def fold_homoglyphs(text: str) -> tuple[str, list[str]]:
    """Map non-Latin look-alikes inside mostly-Latin words to Latin. Returns (text, offending words)."""
    hits: list[str] = []

    def repl(m: re.Match) -> str:
        w = m.group(0)
        scripts = {_script(c) for c in w} - {""}
        if len(scripts) < 2 or "LATIN" not in scripts:
            return w
        n_lat = sum(1 for c in w if _script(c) == "LATIN")
        if n_lat * 2 < sum(1 for c in w if c.isalpha()):  # mostly Cyrillic word with a Latin slip: keep
            return w
        out = []
        changed = False
        for c in w:
            if _script(c) in ("CYRILLIC", "GREEK"):
                lat = _latin_lookalike(c)
                if lat:
                    out.append(lat)
                    changed = True
                    continue
            out.append(c)
        if changed:
            hits.append(w)
        return "".join(out)

    return _WORD.sub(repl, text), hits


def light(text: str) -> str:
    """Normalisation for embeddings / display / entities. Idempotent."""
    if not text:
        return ""
    try:
        import ftfy

        text = ftfy.fix_text(text)
    except Exception:  # noqa: BLE001
        pass
    text = unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH)
    text, _ = fold_homoglyphs(text)
    return _WS.sub(" ", text).strip()


@lru_cache(maxsize=8192)
def _fold_char(c: str) -> str:
    if c in _EXTRA_FOLD:
        return _EXTRA_FOLD[c]
    if c.isalnum():
        low = c.lower()
        base = unicodedata.normalize("NFD", low)[0] if low else c
        if not base.isalnum():
            return " "
        return base
    return " "


def fold(text: str) -> str:
    """Aggressive, length-preserving fold: lowercase, no diacritics, non-alnum -> space."""
    return "".join(_fold_char(c) for c in text)


def fold_pattern(text: str) -> str:
    """Lowercase + strip diacritics but keep regex metacharacters (for lexicon patterns)."""
    return "".join(_fold_char(c) if c.isalnum() else c for c in text)


@dataclass(frozen=True)
class Token:
    text: str   # folded
    start: int  # offset in the source string
    end: int


def tokens(text: str) -> list[Token]:
    """Folded word tokens with offsets into `text` (works on light text thanks to fold() length rule)."""
    f = fold(text)
    return [Token(m.group(0), m.start(), m.end()) for m in re.finditer(r"[a-z0-9]+", f)]


# ------------------------------------------------------------------------------- obfuscation


@dataclass(frozen=True)
class Obfuscation:
    technique: str  # zero_width | homoglyph_mixed_script | fullwidth | leetspeak | combining_marks
    example: str
    count: int


_LEET_WORD = re.compile(r"(?<![\w@$])[^\W\d_]{1,}[013457$][^\W\d_]{1,}(?:[013457$]?[^\W\d_]+)*(?![\w@$])")


def detect_obfuscation(raw: str, ocr: bool = False) -> list[Obfuscation]:
    """Deliberate filter-evasion signals in the RAW (pre-normalisation) text.

    OCR output: fullwidth and zero-width are engine artefacts (RapidOCR's dictionary emits fullwidth
    letters), so only mixed-script homoglyphs and leetspeak are reported for OCR text.
    """
    out: list[Obfuscation] = []
    if not raw:
        return out
    if not ocr:
        zw = [c for c in raw if ord(c) in ZERO_WIDTH and c != "­"]
        if zw:
            out.append(Obfuscation("zero_width", f"U+{ord(zw[0]):04X}", len(zw)))
        fw = [c for c in raw if 0xFF01 <= ord(c) <= 0xFF5E]
        if fw:
            out.append(Obfuscation("fullwidth", fw[0], len(fw)))
        comb = re.findall(r"[^\W\d_][̀-ͯ]{2,}", unicodedata.normalize("NFC", raw))
        if comb:
            out.append(Obfuscation("combining_marks", comb[0], len(comb)))
    _, homo = fold_homoglyphs(unicodedata.normalize("NFKC", raw))
    if homo:
        out.append(Obfuscation("homoglyph_mixed_script", homo[0], len(homo)))
    leet = [w for w in _LEET_WORD.findall(raw) if len(w) >= 5 and sum(c.isalpha() for c in w) >= 4]
    if leet:
        out.append(Obfuscation("leetspeak", leet[0], len(leet)))
    return out


# ------------------------------------------------------------------------------- language

_STOP = {
    "pl": "i w na z do nie się że to jest jak po od o co ale za dla już tak czy jego jej przez może są być "
          "który która które tylko bardzo oraz ten ta te także jeszcze został pan pani swoje ich mnie",
    "en": "the and to of a in is that for it you with on are this be your as have at or from by not was "
          "we can all our more will",
    "de": "der die und das ist nicht mit sich auf für ein eine dem den zu von wir sie auch",
    "fr": "le la les et des est une pour que pas dans sur avec vous qui sont plus",
    "es": "el los las que del por una con para como pero más este esta",
    "it": "il che della per una sono non con gli questo del alla nel",
    "uk": "і в на що не з до це як та для від але ви ми він вона їх",
    "ru": "и в на что не с по это как для от но вы мы он она их был",
}
_STOPSETS = {k: set(v.split()) for k, v in _STOP.items()}
_PL_CHARS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def guess_language(text: str) -> tuple[str | None, float]:
    """Stopword vote. Returns (iso code or None, confidence 0..1). Short text -> (None, 0)."""
    words = [w.lower() for w in re.findall(r"[^\W\d_]+", text)]
    if len(words) < 4:
        return None, 0.0
    scores = {k: sum(1 for w in words if w in s) for k, s in _STOPSETS.items()}
    scores["pl"] += min(sum(1 for c in text if c in _PL_CHARS), len(words)) // 3
    best = max(scores, key=lambda k: scores[k])
    total = sum(scores.values())
    if scores[best] < 2 or total == 0:
        return None, 0.0
    return best, round(scores[best] / total, 3)
