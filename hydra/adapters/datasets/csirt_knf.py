"""CSIRT KNF monthly fraud reviews ("Przegląd wybranych oszustw internetowych", cebrf.knf.gov.pl, Urząd Komisji
Nadzoru Finansowego): Polish write-ups of the month's fraud campaigns (fake investment ads on Facebook, bank
impersonation, parcel / e-TOLL / tax lures, fake shops, malware) with annotated screenshots of the ads, phishing
pages and SMS / e-mails. Bounded snapshot: the 30 most recent Polish reviews (May 2024 - Oct 2026).

Layout (fetched by a polite script outside the repo): `manifest.json` (`reviews[]`: slug, month, year, url,
retrieved, sha256, `images[]` with `src` as written in the page and the local `path`), `pages/<slug>.html` (the
article page as served) and `images/<slug>/<file>` (images embedded in the article body).

Parsing is regex-only on the article body (`itemprop="articleBody"` div; never rendered). The review is cut into
cases at its upper-case `<strong>` headings (each heading is one campaign / scheme named by CSIRT KNF, e.g. "BEZPIECZNA
WERYFIKACJA TOŻSAMOŚCI" or "PODSZYCIE POD BANK PEKAO"). One embedded figure = one incident: `ad_text.txt` (role text)
= case heading + the case's description paragraphs + the figure caption ("Rysunek N ..."); the image goes to
`ad_creative.<ext>` when the caption says it is an ad ("reklam"), else `screenshot.<ext>` with role
`landing_screenshot` (pages, forms) or `feed_screenshot` (SMS / e-mail / messenger screenshots). A named case
without any figure is one text-only incident (section intros such as "PODSZYCIA BEZPOŚREDNIO POD BANKI" are not
cases). Grouping: `label = ext:csirt-knf:<yyyy-mm>/<case-slug>` (figures of one named campaign of one month),
`group_label = ext:csirt-knf:<scheme>` (keyword scheme on heading, then text: investment, bank,
delivery, road_fine, tax_gov, utility_telco, marketplace_shop, account_service, malware, prize_charity, other).
The reviews name no attacker URLs (only reference links to knf.gov.pl / gov.pl), so no `url.txt`.
`meta.lang = "pl"`. Text is KNF's; screenshots show third-party criminal material (impersonated brands and
public figures): EVAL-ONLY, server-side only, never redistributed, aggregates only in public material.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    AD_CREATIVE_STEM,
    AD_TEXT_FILE,
    SCREENSHOT_STEM,
    DatasetInfo,
    ExtIncident,
    Row,
    image_ext,
    norm_label,
    prefixed,
    slug,
    unique_key,
)

INFO = DatasetInfo(
    name="csirt-knf", title="CSIRT KNF monthly fraud reviews (Polish case write-ups + screenshots)",
    homepage="https://cebrf.knf.gov.pl/komunikaty/artykuly-csirt-knf",
    licence="no open licence; KNF nota prawna: reuse requires citing the source with a link to www.knf.gov.pl; "
            "screenshots are third-party criminal material",
    verdict="EVAL-ONLY", modality="text + ad / page / SMS screenshots", label_kind="campaign_case",
    dir="csirt-knf", citation="CSIRT KNF, Przegląd wybranych oszustw internetowych (cebrf.knf.gov.pl), monthly",
)
MONTHS = ("styczen", "luty", "marzec", "kwiecien", "maj", "czerwiec", "lipiec", "sierpien", "wrzesien",
          "pazdziernik", "listopad", "grudzien")
MAX_CASE_TEXT = 6000
MIN_IMG_BYTES = 2048
BLOCK_RE = re.compile(r"<(p|h[1-6]|li)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
IMG_RE = re.compile(r"<img\b[^>]*?\bsrc=\"([^\"]+)\"", re.I)
STRONG_START_RE = re.compile(r"^\s*(?:&nbsp;|\s)*<(strong|b)\b[^>]*>(.*?)</\1\s*>(.*)$", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
CAPTION_RE = re.compile(r"^(?:rysunek|rys\.)\s*\d+", re.I)
DIV_RE = re.compile(r"<(/?)div\b", re.I)
UMBRELLA_RE = re.compile(r"^podszycia\b.*\bpod banki$|^zidentyfikowane kampanie", re.I)
SKIP_HEAD_RE = re.compile(r"^(?:rys|raport roczny|rekomendacje dla|przegląd wybranych)", re.I)
# scheme keywords (checked in order on heading + case text, lower-cased)
SCHEMES: tuple[tuple[str, str], ...] = (
    ("investment", r"inwestyc|lokat|oprocentowan|zysk|kryptowalut"),
    ("malware", r"złośliw|malware|aplikacj\w* mobiln|\bapk\b|fvncbot|zainstal"),
    ("delivery", r"paczk|przesyłk|inpost|poczt|dpd|dhl|dostaw|kurier|celn"),
    ("road_fine", r"e-toll|mandat|wykroczen|drogow|przejazd|parkow|fotoradar"),
    ("tax_gov", r"podat|e-urząd|skarbow|narodow\w* fundusz|krajow\w* administracj|gov\.pl|pesel|mobywatel|nfz|"
                r"refundac|zus|socjaln"),
    ("bank", r"bank|blik|pko|pekao|santander|mbank|\bing\b|alior|millennium|bnp|credit agricole|\bsgb\b|\bbps\b|"
             r"boś|\biko\b|ipko|peopay|velo|nest|citi|getin|erste|kart\w* płatnicz"),
    ("utility_telco", r"energi|pge|tauron|enea|gaz|doładow|orange|play|plus|t-mobile|abonament|hosting|domen"),
    ("marketplace_shop", r"sklep|olx|allegro|vinted|okazj|9 zł|black friday|bilet|kupon|promocj|tiktok"),
    ("account_service", r"facebook|whatsapp|instagram|netflix|disney|spotify|booking|poczt\w* e-mail|home\.pl|"
                        r"konto|hasł|logowan"),
    ("prize_charity", r"nagrod|wygraj|ankiet|zbiórk|bon\w* paliw|karta paliw|kart\w* miejsk"),
)
IMPERSONATED_RE = re.compile(r"podsz\w*\s+(?:się\s+)?pod\s+([^,.;:()\n–-]{2,40})", re.I)
GENERIC_RE = re.compile(r"^(?:znan|polsk|bank\w*$|banki|instytuc|komunikacj|pracownik|urz[ąę]d$|firm|operator|sklep)",
                        re.I)
PL_ASCII = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def _text(fragment: str) -> str:
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", fragment))).strip()


def article_body(page: str) -> str:
    """Inner HTML of the `itemprop="articleBody"` div (div depth counted; regex only)."""
    i = page.find('itemprop="articleBody"')
    if i < 0:
        return ""
    start = page.find(">", i) + 1
    depth = 1
    for m in DIV_RE.finditer(page, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[start:m.start()]
    return page[start:]


def _is_heading(t: str) -> bool:
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 4 or len(t) > 140 or SKIP_HEAD_RE.match(t):
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.75


def parse_review(page: str) -> list[dict]:
    """Cases of one review: [{heading, paras: [...], figures: [{srcs: [...], caption}]}] in page order."""
    cases: list[dict] = []
    cur: dict | None = None
    pending: list[str] = []

    def flush_uncaptioned() -> None:
        if cur is not None and pending:
            cur["figures"].extend({"src": s, "caption": ""} for s in pending)
        pending.clear()

    for m in BLOCK_RE.finditer(article_body(page)):
        inner = m.group(2)
        srcs = IMG_RE.findall(inner)
        head, rest = None, inner
        sm = STRONG_START_RE.match(inner)
        if sm and not srcs:
            cand = _text(sm.group(2))
            if _is_heading(cand):
                head, rest = cand, sm.group(3)
        if head is not None:
            flush_uncaptioned()
            cur = {"heading": head, "paras": [], "figures": []}
            cases.append(cur)
        pending.extend(html.unescape(s) for s in srcs)
        t = _text(rest)
        if not t:
            continue
        if CAPTION_RE.match(t):
            if cur is not None:
                if pending:
                    cur["figures"].extend({"src": s, "caption": t} for s in pending)
                else:
                    cur["paras"].append(t)
            pending.clear()
        else:
            flush_uncaptioned()
            if cur is not None:
                cur["paras"].append(t)
    flush_uncaptioned()
    return cases


def scheme(*texts: str) -> str:
    """First scheme whose keywords hit, trying the texts in order (case heading before description)."""
    for t in texts:
        low = t.lower()
        for name, pat in SCHEMES:
            if re.search(pat, low):
                return name
    return "other"


def _impersonated(*texts: str) -> str | None:
    for t in texts:
        for m in IMPERSONATED_RE.finditer(t or ""):
            v = norm_label(" ".join(m.group(1).strip(" \"'„”").split()[:4]))
            if v and not GENERIC_RE.match(v):
                return v
    return None


def _manifest(root: Path) -> list[dict]:
    p = root / "manifest.json"
    if not p.is_file():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("reviews", [])


def index(root: Path) -> list[Row]:
    out: list[Row] = []
    for rev in _manifest(root):
        s = rev.get("slug") or ""
        page_p = root / "pages" / f"{s}.html"
        if not page_p.is_file():
            continue
        month = rev.get("month")
        ym = f"{rev.get('year')}-{MONTHS.index(month) + 1:02d}" if month in MONTHS else str(rev.get("year"))
        imgs = {i["src"]: i["path"] for i in rev.get("images", []) if i.get("path")}
        imgs.update({html.unescape(k): v for k, v in list(imgs.items())})
        cases = parse_review(page_p.read_text(encoding="utf-8", errors="replace"))
        seen: dict[str, int] = {}
        for c in cases:
            cslug = slug(c["heading"].lower().translate(PL_ASCII), 40)
            seen[cslug] = seen.get(cslug, 0) + 1
            if seen[cslug] > 1:
                cslug = f"{cslug}-{seen[cslug]}"
            desc = "\n\n".join(c["paras"])[:MAX_CASE_TEXT]
            label = f"{ym}/{cslug}"
            grp = scheme(c["heading"], desc)
            base = {"review": s, "review_url": rev.get("url"), "ym": ym, "heading": c["heading"], "desc": desc,
                    "lang": "pl"}
            figs = [f for f in c["figures"] if f["src"] in imgs]
            if not figs:
                if desc and not UMBRELLA_RE.match(c["heading"]):  # section intros are not cases
                    out.append(Row(f"{ym}/{cslug}/text", label, grp, {**base, "caption": "", "image": None}))
                continue
            for k, f in enumerate(figs):
                out.append(Row(f"{ym}/{cslug}/{k:02d}", label, grp,
                               {**base, "caption": f["caption"], "image": imgs[f["src"]]}))
    return out


def where(scheme: str | None = None, with_image: bool | None = None, ym: str | None = None):
    if scheme is None and with_image is None and ym is None:
        return None
    return lambda r: ((scheme is None or r.group == scheme) and (ym is None or r.extra.get("ym") == ym)
                      and (with_image is None or bool(r.extra.get("image")) == with_image))


def figure_kind(caption: str) -> str:
    low = caption.lower()
    if "reklam" in low:
        return "ad"
    if re.search(r"\bsms|wiadomoś|e-mail|\bmail|messenger|whatsapp|powiadomien", low):
        return "message"
    return "page"


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        e = r.extra
        text = e["heading"] + "\n\n" + e["desc"]
        if e.get("caption"):
            text += "\n\n" + e["caption"]
        files: dict[str, bytes | Path] = {AD_TEXT_FILE: (text.strip() + "\n").encode("utf-8")}
        roles: dict[str, str] = {}
        kind = None
        if e.get("image"):
            p = root / e["image"]
            if not p.is_file() or p.stat().st_size < MIN_IMG_BYTES:
                continue
            data = p.read_bytes()
            ext = image_ext(data)
            if ext == ".bin":
                continue
            kind = figure_kind(e.get("caption") or "")
            if kind == "ad":
                files[AD_CREATIVE_STEM + ext] = data
            else:
                files[SCREENSHOT_STEM + ext] = data
                roles[SCREENSHOT_STEM + ext] = "landing_screenshot" if kind == "page" else "feed_screenshot"
        yield ExtIncident(
            dataset=INFO.name, key=unique_key(r.key), files=files, label=prefixed(INFO.name, r.label),
            group_label=prefixed(INFO.name, r.group), roles=roles,
            meta={"lang": "pl", "review": e.get("review"), "review_url": e.get("review_url"), "month": e.get("ym"),
                  "case_heading": e.get("heading"), "scheme": r.group, "figure_kind": kind,
                  "caption": e.get("caption") or None,
                  "impersonated": _impersonated(e.get("caption") or "", e.get("heading", ""), e.get("desc", "")),
                  "label_kind": INFO.label_kind},
        )
