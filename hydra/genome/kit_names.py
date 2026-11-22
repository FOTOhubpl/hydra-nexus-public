"""Kit file-name-set fingerprint (docs/cleanroom/kit_filetree_fingerprint.md, clean-room builder implementation).

Scam kits travel as archives with a characteristic set of file and folder names. Saved pages (`_files/`, capture
`resources/`) and first-party HAR request paths keep those names even when the bytes change (re-minified JS,
recompressed images), so `asset_sha256` differs while the name set stays. Extractor `inf_kit_names` emits:

- `asset_nameset_hash`: hash of the sorted distinctive names (exact family match), quality 0.6;
- `asset_name_token`: one gene per distinctive name, quality 0.4, rarity-weighted one by one.

Both live in `inf:kit:asset_names` (kit tier, one bit budget, capped with the other kit evidence). Names that carry
no kit information are dropped: library files, content-hash or random names, numeric names, generic names
(index.html, style.css, ...) and CMS path families (wp-content, ...). Fewer than 4 distinctive names: the gene is
`unavailable` (single-file pages, hashed capture names), never evidence against a link.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from urllib.parse import urlsplit

from hydra.contracts import Family, GeneStatus
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.dom import _clean_name
from hydra.genome.webparse import LIB_NAME, GeneSink, bundle, first_party_hosts, h16

MIN_NAMES = 4
MAX_TOKENS = 40
ENTROPY_MAX, ENTROPY_MIN_LEN = 3.5, 16
GROUP = "inf:kit:asset_names"

KIND_PL_NAMES = {
    "asset_nameset_hash": "Ten sam zestaw nazw plików strony (ten sam kit)",
    "asset_name_token": "Wspólna nietypowa nazwa pliku strony",
}

# Our own lists (not taken from any rule set): generic web names and CMS / framework path families.
GENERIC = frozenset("""
index.html index.htm index.php index default.htm default.html main.js main.css style.css styles.css app.js app.css
script.js scripts.js common.js custom.js custom.css favicon.ico favicon.png logo.png logo.svg logo.jpg icon.png
apple-touch-icon.png manifest.json robots.txt sitemap.xml bundle.js vendor.js runtime.js polyfills.js chunk.js
image.png image.jpg img.png photo.jpg banner.jpg bg.jpg background.jpg arrow.svg close.svg menu.svg play.svg
loader.gif spinner.gif blank.gif pixel.gif 1x1.gif css js img images assets static fonts media files resources
dist build public src lib libs vendor uploads content data api v1 v2 index.css index.js print.css reset.css
normalize.css fonts.css font.css icons.css icons.svg sprite.svg sprite.png thumb.jpg preview.jpg video.mp4 poster.jpg
""".split())
CMS_PATHS = re.compile(r"^(wp-content|wp-includes|wp-admin|wp-json|themes|plugins|elementor|woocommerce|sites|"
                       r"node_modules|_next|_nuxt|__webpack|webpack|cdn-cgi|gtag|gtm|ajax|cache|tmp)$", re.I)
LIB_EXTRA = re.compile(r"^(jquery|bootstrap|popper|swiper|slick|owl|lightbox|fancybox|select2|moment|lodash|"
                       r"underscore|modernizr|respond|html5shiv|polyfill|core-js|regenerator|tether|wow|aos|gsap|"
                       r"three|chart|d3|vue|react|angular|zepto|hammer|fontawesome|font-awesome|glyphicons|"
                       r"materialdesignicons|roboto|open-sans|opensans|montserrat|lato|poppins|inter|fbevents|"
                       r"recaptcha|turnstile|hcaptcha|analytics|gtag|pixel|tracking)", re.I)
HEXISH = re.compile(r"^[0-9a-f]{8,}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _entropy(s: str) -> float:
    c = Counter(s)
    n = len(s)
    return -sum(v / n * math.log2(v / n) for v in c.values())


def distinctive(name: str) -> str | None:
    """Lower-cased cleaned name, or None when it carries no kit information."""
    n = _clean_name(name.split("?")[0].split("#")[0])
    if not n or len(n) < 3 or n in GENERIC or CMS_PATHS.match(n):
        return None
    stem = n.rsplit(".", 1)[0] if "." in n else n
    if not stem or stem.replace("-", "").replace("_", "").replace(".", "").isdigit():
        return None
    if LIB_NAME.match(n) or LIB_EXTRA.match(n):
        return None
    core = re.sub(r"[^a-z0-9]", "", stem)
    # entropy test only for unseparated names: "baner-prezes-wywiad" is descriptive, "Xk9qL2vR8tZ0pW3m" is random
    if HEXISH.match(stem) or (len(core) >= ENTROPY_MIN_LEN and _entropy(core) > ENTROPY_MAX
                              and not re.search(r"[-_ ]", stem)):
        return None
    if re.search(r"[0-9a-f]{12,}", stem):  # embedded content hash: app.3f9a2c1b7d4e.js
        return None
    return n


def names_of(inc: ResolvedIncident) -> dict[str, tuple[str, str | None, int | None]]:
    """Distinctive name -> (source path/url, artifact id, har entry)."""
    b = bundle(inc)
    out: dict[str, tuple[str, str | None, int | None]] = {}
    for a, _data in b.assets:
        path = a.meta.path_in_incident
        d = distinctive(path.rsplit("/", 1)[-1])
        if d:
            out.setdefault(d, (path[-200:], a.meta.artifact_id, None))
    fp = first_party_hosts(b)
    for h in b.hars:
        if h.local_replay:
            continue
        for r in h.requests:
            host = (r.get("host") or "").lower()
            if host not in fp:
                continue
            parts = [p for p in urlsplit(r.get("url") or "").path.split("/") if p]
            if not parts:
                continue
            for seg in [parts[-1]] + parts[-3:-1]:
                d = distinctive(seg)
                if d:
                    out.setdefault(d, ((r.get("url") or "")[:200], h.artifact_id, r.get("entry")))
    return out


@register("inf_kit_names", family=Family.INF, version=1)
def inf_kit_names(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, "inf_kit_names@1")
    names = names_of(inc)
    if len(names) < MIN_NAMES:
        sink.missing("asset_nameset_hash", GeneStatus.UNAVAILABLE,
                     f"{len(names)} distinctive asset names (< {MIN_NAMES}): no kit name set", GROUP)
        return sink.result()
    ordered = sorted(names)
    first = names[ordered[0]]
    sink.add("asset_nameset_hash", h16("|".join(ordered)), GROUP, artifact_id=first[1], quality=0.6,
             note=f"{len(ordered)} names: " + ", ".join(ordered)[:260])
    for n in ordered[:MAX_TOKENS]:
        src, aid, entry = names[n]
        sink.add("asset_name_token", n, GROUP, artifact_id=aid, har_entry=entry,
                 dom_path=None if entry is not None else src, quality=0.4, note=f"from {src}")
    return sink.result()


def jaccard(a: set[str], b: set[str]) -> float:
    """Name-set Jaccard for explanations (variant kits: 1 of 8 names changed -> about 0.78)."""
    return len(a & b) / len(a | b) if a or b else 0.0
