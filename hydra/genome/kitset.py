"""G06: normalised kit file-set of a saved landing page (MinHash/LSH bands scorable by the rarity scorer).

Own implementation from the paper ideas in docs/research/GAPS_PAPERS.md §8 (kit families by first-party file
overlap, P01; kit ground truth by Jaccard over normalised file hashes, P10) and the clean-room behaviour spec
docs/cleanroom/kit_filetree_fingerprint.md (file-name set). No third-party code.

One file set per saved page: a `<name>_files/` (or capture `resources/`) folder of landing assets, or the
first-party response bodies of one HAR. Each first-party file contributes
- a content element `c:<h16>` of its normalised bytes (JS/HTML: `norm_js`, which masks ids, e-mails, URLs, IPs
  and phones; CSS: comments, whitespace and `url()` removed; other files: raw bytes), and
- a name element `n:<name>` when its normalised name is distinctive (not generic, not hash-like, not numeric).
Skipped: libraries (`_is_lib`), CMS core and Cloudflare `cdn-cgi` helpers, web fonts, files < 64 bytes and IP
blocklists (text files whose tokens are >= 90% IPv4/IPv6 addresses or CIDR ranges).

Genes (all INF):
- `kit_fileset_band` (dependency group `inf:kit:kit_fileset`): LSH band values of a 128-permutation MinHash over the
  element set (16 bands x 8 rows). Two sets share a band with probability J^8 per band, so pairs sharing more of
  the set share more bands; the scorer pools them in one group (max + 0.25 x rest) and weights each band by df.
  With fewer than MIN_FILES files the gene is `not_applicable` (unknown, never negative).
- `kit_file` (dependency group `inf:boilerplate:kit_fileset_member`, 0 bits): the set members, kept so that
  `fileset_overlap` can report exact Jaccard / containment and the shared file names for explanations.

Thresholds (MIN_FILES = 4, the LSH S-curve midpoint (1/16)^(1/8) ~ 0.71) are assumptions borrowed from P01/P10,
not validated on NASK data.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from hydra.contracts import Gene, GeneStatus

MIN_FILES = 4
MAX_FILES = 150  # members per set kept as genes (largest files are not preferred: sorted by element)
MIN_FILE_BYTES = 64
BANDS = 16
ROWS = 8
N_PERM = BANDS * ROWS
BLOCKLIST_RATIO = 0.9
BLOCKLIST_MIN_TOKENS = 5
BAND_KIND = "kit_fileset_band"
MEMBER_KIND = "kit_file"
BAND_GROUP = "inf:kit:kit_fileset"
MEMBER_GROUP = "inf:boilerplate:kit_fileset_member"

FONT_EXT = {"woff", "woff2", "ttf", "otf", "eot"}
TEXT_EXT = {"js", "mjs", "css", "html", "htm", "json", "txt", "php", "xml", "svg", "csv", "conf", "ini", "htaccess"}
# CMS core / CDN helper files that unrelated sites share byte-for-byte (own list, not exhaustive). Also treated as
# libraries by dom._is_lib (asset_sha256_lib, boilerplate), so their hashes never give kit bits.
SKIP_NAME = re.compile(
    r"^(wp-|wp_|comment-reply|block-library|regenerator-runtime|jquery-migrate|dashicons|admin-bar|twemoji|"
    r"emoji-release|elementor-|frontend-modules|woocommerce|email-decode|rocket-loader|challenge-platform|"
    r"(hooks|i18n|dom-ready|a11y|polyfill|beacon|invisible|underscore|backbone)(\.min)?\.js$)",
    re.I,
)
# Names that carry no kit information on their own (content still counts)
GENERIC_NAMES = {
    "index",
    "main",
    "style",
    "styles",
    "app",
    "script",
    "scripts",
    "bundle",
    "vendor",
    "common",
    "custom",
    "favicon",
    "logo",
    "image",
    "img",
    "photo",
    "banner",
    "background",
    "bg",
    "icon",
    "default",
    "global",
    "site",
    "theme",
    "page",
    "home",
    "base",
    "chunk",
    "runtime",
    "manifest",
    "config",
    "data",
    "font",
    "fonts",
}
_HASHY = re.compile(r"^[0-9a-f]{12,}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_P61 = (1 << 61) - 1


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def norm_file_name(name: str) -> str:
    """Lower-case base name without query, browser save suffixes, content hashes, versions and `.min`."""
    n = name.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1].strip().lower()
    n = re.sub(r"\.(pobrane|pobierz|download)$", "", n)
    n = re.sub(r"\s*\(\d+\)", "", n)
    stem, dot, ext = n.rpartition(".")
    if not dot:
        stem, ext = n, ""
    stem = re.sub(r"([.\-_~])[0-9a-f]{6,}$", "", stem)  # app.3f9a8b2c / app-3f9a8b2c (build hash)
    stem = re.sub(r"_[0-9a-f]{8}$", "", stem)  # page-capture resources/<name>_<sha8>
    stem = re.sub(r"([.\-_@]v?\d+(\.\d+)+)$", "", stem)  # versions
    stem = re.sub(r"\.min$", "", stem)
    stem = re.sub(r"([.\-_~])[0-9a-f]{6,}$", "", stem)
    return f"{stem}.{ext}" if ext else stem


def _entropy(s: str) -> float:
    c = Counter(s)
    return -sum(v / len(s) * math.log2(v / len(s)) for v in c.values()) if s else 0.0


def distinctive_name(name: str) -> bool:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if len(stem) < 3 or stem in GENERIC_NAMES or stem.isdigit() or _HASHY.match(stem):
        return False
    if re.fullmatch(r"(saved_resource|resource|file|img|image|photo)[\d_\-]*", stem):
        return False
    return not (len(stem) >= 16 and _entropy(stem) > 3.5)


def _ip_token(t: str) -> bool:
    try:
        if "/" in t:
            ipaddress.ip_network(t, strict=False)
        else:
            ipaddress.ip_address(t)
        return True
    except ValueError:
        return False


def is_ip_blocklist(text: str) -> bool:
    """>= 90% of the tokens are IPv4/IPv6 addresses or CIDR ranges (cloaker / bot blocklists)."""
    toks = [t for t in re.split(r"[\s,;'\"`\[\](){}|]+", text[:2_000_000]) if t]
    if len(toks) < BLOCKLIST_MIN_TOKENS:
        return False
    return sum(1 for t in toks if _ip_token(t)) / len(toks) >= BLOCKLIST_RATIO


def norm_css(txt: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    css = re.sub(r"url\([^)]*\)", "url()", css)
    return re.sub(r"\s+", "", css)


def content_key(name: str, data: bytes) -> str | None:
    """h16 of the normalised content, or None when the file is skipped (IP blocklist)."""
    from hydra.genome.webparse import h16, norm_js

    ext = _ext(name)
    if ext in TEXT_EXT or not ext:
        txt = data.decode("utf-8", errors="replace")
        if is_ip_blocklist(txt):
            return None
        if ext == "css":
            return h16(norm_css(txt))
        if ext in ("js", "mjs", "html", "htm", "json", "php", ""):
            return h16(norm_js(txt))
        return h16(re.sub(r"\s+", "", txt))
    return hashlib.sha256(data).hexdigest()[:16]


def skip_file(name: str, size: int, *, lib: bool, path: str = "") -> str | None:
    """Reason a file is not a kit member, else None."""
    if lib:
        return "library"
    if "cdn-cgi" in path.lower() or SKIP_NAME.match(name):
        return "cms_or_cdn_helper"
    if _ext(name) in FONT_EXT:
        return "font"
    if size < MIN_FILE_BYTES:
        return "tiny"
    return None


@dataclass
class FileSet:
    """Kit members of one saved page."""

    key: str  # set id (h16 of the folder / HAR id, first 8 chars)
    where: str  # folder path or "har:<artifact>"
    artifact_id: str | None
    files: dict[str, str] = field(default_factory=dict)  # content element -> display name
    names: set[str] = field(default_factory=set)  # distinctive name elements
    skipped: Counter = field(default_factory=Counter)

    def add(self, raw_name: str, data: bytes, *, lib: bool, path: str = "") -> None:
        name = norm_file_name(raw_name)
        why = skip_file(name, len(data), lib=lib, path=path)
        if why is None:
            ck = content_key(name, data)
            if ck is None:
                why = "ip_blocklist"
            else:
                self.files.setdefault("c:" + ck, name)
                if distinctive_name(name):
                    self.names.add("n:" + name)
                return
        self.skipped[why] += 1

    @property
    def n_files(self) -> int:
        return len(self.files)

    def elements(self) -> set[str]:
        return set(self.files) | self.names


def _h64(e: str) -> int:
    return int.from_bytes(hashlib.blake2b(e.encode("utf-8", "replace"), digest_size=8).digest(), "big")


def _perm_params() -> list[tuple[int, int]]:
    out = []
    for i in range(N_PERM):
        d = hashlib.sha256(f"hydra-kit-fileset-perm-{i}".encode()).digest()
        out.append((int.from_bytes(d[:8], "big") % (_P61 - 1) + 1, int.from_bytes(d[8:16], "big") % _P61))
    return out


_PERMS = _perm_params()


def minhash(elements: Iterable[str]) -> list[int]:
    xs = [_h64(e) for e in elements]
    return [min((a * x + b) % _P61 for x in xs) for a, b in _PERMS]


def lsh_bands(sig: list[int]) -> list[str]:
    out = []
    for i in range(BANDS):
        rows = ",".join(str(v) for v in sig[i * ROWS : (i + 1) * ROWS])
        out.append(f"b{i:02d}:{hashlib.sha256(rows.encode()).hexdigest()[:16]}")
    return out


def emit(sink, fs: FileSet) -> None:  # sink: webparse.GeneSink
    """Band genes (or a not_applicable gene) and 0-bit member genes for one file set."""
    skipped = ", ".join(f"{k}={v}" for k, v in sorted(fs.skipped.items()))
    if fs.n_files < MIN_FILES:
        sink.add(
            BAND_KIND,
            None,
            BAND_GROUP,
            artifact_id=fs.artifact_id,
            dom_path=fs.where[-200:],
            status=GeneStatus.NOT_APPLICABLE,
            note=f"{fs.n_files} first-party files < {MIN_FILES}; skipped: {skipped or 'none'}",
        )
        return
    els = fs.elements()
    note = f"set={fs.key} files={fs.n_files} names={len(fs.names)} minhash={BANDS}x{ROWS} skipped: {skipped or 'none'}"
    for band in lsh_bands(minhash(els)):
        sink.add(BAND_KIND, band, BAND_GROUP, artifact_id=fs.artifact_id, dom_path=fs.where[-200:], note=note)
    for e in sorted(els)[: 2 * MAX_FILES]:
        sink.add(
            MEMBER_KIND,
            f"{fs.key}:{e}",
            MEMBER_GROUP,
            artifact_id=fs.artifact_id,
            dom_path=fs.where[-200:],
            note=fs.files.get(e, e[2:]),
            quality=0.0,
        )


def file_set_key(where: str) -> str:
    return hashlib.sha256(where.encode()).hexdigest()[:8]


def page_folder(path_in_incident: str) -> str:
    """The saved-page folder of an asset: path up to the first `*_files` or `resources` segment, else its dir."""
    parts = path_in_incident.split("/")
    for i, p in enumerate(parts[:-1]):
        pl = p.lower()
        if pl.endswith("_files") or pl in ("resources", "_files"):
            return "/".join(parts[: i + 1])
    return "/".join(parts[:-1])


# ------------------------------------------------------------------------------------------ explanations


def _sets(genes: Iterable[Gene]) -> dict[str, tuple[set[str], dict[str, str]]]:
    out: dict[str, tuple[set[str], dict[str, str]]] = {}
    for g in genes:
        if g.kind != MEMBER_KIND or g.value is None or ":" not in g.value:
            continue
        key, el = g.value.split(":", 1)
        s, names = out.setdefault(key, (set(), {}))
        s.add(el)
        names[el] = g.evidence.note or el[2:]
    return out


def fileset_overlap(genes_a: Iterable[Gene], genes_b: Iterable[Gene]) -> dict | None:
    """Exact overlap of the best-matching file sets of two incidents (from their `kit_file` genes).

    Returns {jaccard, containment, shared: [file names], n_a, n_b, set_a, set_b} or None when either side has no
    file set. Jaccard and containment are over all elements (content + distinctive names); n_a / n_b count files
    (distinct content elements); containment = |A and B| / min(|A|, |B|)."""
    sa, sb = _sets(genes_a), _sets(genes_b)
    best: dict | None = None
    for ka, (ea, na) in sa.items():
        for kb, (eb, _nb) in sb.items():
            inter = ea & eb
            union = ea | eb
            jac = len(inter) / len(union) if union else 0.0
            cont = len(inter) / min(len(ea), len(eb)) if ea and eb else 0.0
            if best is not None and (jac, cont) <= (best["jaccard"], best["containment"]):
                continue
            shared = sorted({na.get(e, e[2:]) for e in inter})
            best = {
                "jaccard": round(jac, 3),
                "containment": round(cont, 3),
                "shared": shared,
                "n_a": sum(1 for e in ea if e.startswith("c:")),
                "n_b": sum(1 for e in eb if e.startswith("c:")),
                "set_a": ka,
                "set_b": kb,
            }
    return best
