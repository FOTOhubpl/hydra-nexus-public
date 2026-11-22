"""Eval STUB gene extractor (not the product). Used until CORE's `hydra.pipeline` lands, and as a fixed
reference for the baseline ladder. Fast, offline, no ML models: HAR hosts/IPs/trackers/URL skeletons, saved
page title/text shingles/DOM shingles/trackers/links, image pHash + sha256, video key-frame pHash,
`.info.json` platform fields, keyword scheme. Every gene follows `hydra.contracts.Gene`; extractor names are
prefixed `evalstub_` so results can never be confused with CORE's.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from eval.incidents import FileEntry, IncidentFiles, load_info_json
from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene, GeneStatus, Provenance

V = "1"

# Platform / CDN hosts are boilerplate: they say nothing about the campaign (PLAN_MAX §1.8).
BOILERPLATE_DOMAINS = {
    "facebook.net", "facebook.com", "fbcdn.net", "google.com", "google.pl", "googleapis.com", "gstatic.com",
    "googletagmanager.com", "google-analytics.com", "doubleclick.net", "jsdelivr.net", "cloudflare.com",
    "jquery.com", "unpkg.com", "unsplash.com", "youtube.com", "ytimg.com", "apple.com", "mzstatic.com",
    "bootstrapcdn.com", "fontawesome.com", "w3.org", "schema.org", "instagram.com", "tiktok.com", "x.com",
    "twitter.com", "er-api.com", "gazeta.pl", "tvn24.pl", "wp.pl", "onet.pl",
}

_TRACKER_RES = [
    ("fb_pixel", re.compile(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{12,20})['\"]")),
    ("fb_pixel", re.compile(r"facebook\.com/tr/?\?[^\"'\s]*?\bid=(\d{12,20})")),
    ("gtm", re.compile(r"\b(GTM-[A-Z0-9]{5,9})\b")),
    ("ga4", re.compile(r"\b(G-[A-Z0-9]{8,12})\b")),
    ("ua", re.compile(r"\b(UA-\d{4,10}-\d{1,3})\b")),
    ("gads", re.compile(r"\b(AW-\d{8,12})\b")),
    ("ym", re.compile(r"ym\(\s*(\d{6,10})\s*,\s*['\"]init")),
    ("tiktok_pixel", re.compile(r"ttq\.load\(\s*['\"]([A-Z0-9]{15,25})['\"]")),
]
_SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)(\S+?)\s*-->", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+48[\s-]?)?(?:\d{3}[\s-]?\d{3}[\s-]?\d{3})(?!\d)")
_EMAIL = re.compile(r"[\w.+-]{2,}@[\w-]{2,}\.[\w.]{2,}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_WORD = re.compile(r"[^\W\d_]{2,}", re.U)

SCHEMES = {
    "investment": ["inwest", "zarab", "kapitał", "zysk", "krypto", "bitcoin", "platform", "trading", "dochód",
                   "invest", "profit"],
    "health": ["lek", "zdrow", "stawy", "prostat", "cukrzyc", "odchudz", "kapsuł", "suplement", "lekarz",
               "metabo", "uro", "health", "pills"],
    "gambling": ["kasyn", "bonanza", "spin", "slot", "wygran", "bonus", "casino", "jackpot"],
    "fake_news_bait": ["zamach", "wywiad", "aresztow", "skandal", "kajdank", "usunięto", "nagranie", "tragedi",
                       "shocking"],
}

_TLD = None


def _tld():
    global _TLD
    if _TLD is None:
        import tldextract

        _TLD = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
    return _TLD


def registrable(host: str) -> str:
    host = (host or "").lower().strip(".")
    if not host or re.fullmatch(r"[\d.]+", host):
        return host
    ext = _tld()(host)
    if hasattr(ext, "top_domain_under_public_suffix"):
        return ext.top_domain_under_public_suffix
    return ext.registered_domain


def is_boilerplate(host: str) -> bool:
    reg = registrable(host)
    return reg in BOILERPLATE_DOMAINS or not reg or "." not in reg


def _tok(seg: str) -> str:
    if _UUID.fullmatch(seg):
        return "{uuid}"
    if seg.isdigit():
        return "{n}"
    if re.fullmatch(r"[0-9a-f]{16,}", seg, re.I):
        return "{hex}"
    if len(seg) >= 16 and re.search(r"\d", seg) and re.search(r"[A-Za-z]", seg):
        return "{tok}"
    return seg


def url_skeleton(url: str) -> str:
    """Host-free URL shape: path segments with ids generalised + sorted query parameter names."""
    s = urlsplit(url)
    path = "/".join(_tok(p) for p in s.path.split("/"))
    params = sorted({k for k, _ in parse_qsl(s.query, keep_blank_values=True)})
    return f"{path}?{'&'.join(params)}" if params else path


def label_skeleton(host: str) -> str:
    """Shape of the domain label: letters→a, digits→9, runs collapsed (e.g. shop-42deals.test → a-9a.test)."""
    reg = registrable(host)
    if "." not in reg:
        return ""
    label, _, suffix = reg.partition(".")
    shape = re.sub(r"[a-z]+", "a", re.sub(r"\d+", "9", label))
    return f"{shape}.{suffix}"


def normalise_text(t: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKC", t).replace("​", "").replace("­", "")
    return re.sub(r"\s+", " ", t).strip().lower()


def shingles(text: str, n: int = 5, limit: int = 4000) -> set[str]:
    words = _WORD.findall(normalise_text(text))
    out = {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}
    return {hashlib.blake2b(s.encode(), digest_size=8).hexdigest() for s in list(out)[:limit]}


class _G:
    def __init__(self, incident_id: str) -> None:
        self.iid = incident_id
        self.genes: list[Gene] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, family: Family, kind: str, value: str | None, extractor: str, art: FileEntry | None = None,
            prov: Provenance = Provenance.OBS, group: str | None = None, quality: float = 1.0, **ev) -> None:
        if value is None or value == "":
            return
        key = (kind, value)
        if key in self._seen:
            return
        self._seen.add(key)
        self.genes.append(Gene(
            incident_id=self.iid, family=family, kind=kind, value=value, quality=quality, provenance=prov,
            evidence=EvidencePointer(artifact_id=art.sha256 if art else None, **ev),
            extractor=f"evalstub_{extractor}@{V}", dependency_group=group))

    def status(self, family: Family, kind: str, status: GeneStatus, note: str) -> None:
        self.genes.append(Gene(incident_id=self.iid, family=family, kind=kind, status=status,
                               evidence=EvidencePointer(note=note), extractor=f"evalstub_status@{V}"))


def _har(g: _G, f: FileEntry) -> None:
    data = json.loads(f.path.read_text(encoding="utf-8", errors="replace"))
    entries = data.get("log", {}).get("entries", []) or []
    first_party: dict[str, int] = {}
    for i, e in enumerate(entries):
        req = e.get("request", {}) or {}
        url = req.get("url", "") or ""
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            continue
        for kind, rx in _TRACKER_RES:
            for m in rx.findall(url):
                g.add(Family.INF, "tracker_id", f"{kind}:{m}", "har", f, har_entry=i)
        if is_boilerplate(host):
            continue
        first_party[host] = first_party.get(host, 0) + 1
        g.add(Family.INF, "host", host, "har", f, group="inf_host", har_entry=i)
        g.add(Family.INF, "domain", registrable(host), "har", f, group="inf_host", har_entry=i)
        ip = e.get("serverIPAddress")
        if ip:
            g.add(Family.INF, "ip", ip.strip("[]"), "har", f, har_entry=i)
        skel = url_skeleton(url)
        if len(skel) > 3:
            g.add(Family.INF, "url_skeleton", skel, "har", f, group="inf_kit", har_entry=i)
        status = (e.get("response", {}) or {}).get("status")
        loc = (e.get("response", {}) or {}).get("redirectURL")
        if status in (301, 302, 303, 307, 308) and loc:
            g.add(Family.INF, "redirect_host", (urlsplit(loc).hostname or "").lower(), "har", f, har_entry=i)
        for k, _ in parse_qsl(urlsplit(url).query):
            if k.startswith(("utm_", "sub", "click", "aff", "cid", "pid", "tds")):
                g.add(Family.INF, "tds_param", k, "har", f, group="inf_kit", har_entry=i)
    for host in first_party:
        g.add(Family.INF, "domain_shape", label_skeleton(host), "har", f, prov=Provenance.INF)


def _html(g: _G, f: FileEntry) -> None:
    from bs4 import BeautifulSoup

    raw = f.path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    m = _SAVED_FROM.search(text[:4000])
    if m:
        host = (urlsplit(m.group(1)).hostname or "").lower()
        if host and not is_boilerplate(host):
            g.add(Family.INF, "host", host, "html", f, group="inf_host")
            g.add(Family.INF, "domain", registrable(host), "html", f, group="inf_host")
        g.add(Family.INF, "url_skeleton", url_skeleton(m.group(1)), "html", f, group="inf_kit")
    for kind, rx in _TRACKER_RES:
        for v in set(rx.findall(text)):
            g.add(Family.INF, "tracker_id", f"{kind}:{v}", "html", f)
    soup = BeautifulSoup(text, "lxml")
    title = normalise_text(soup.title.get_text(" ")) if soup.title else ""
    if title:
        g.add(Family.TXT, "title", title, "html", f, group="txt_page")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.get_text(" ")
    for s in shingles(body):
        g.add(Family.TXT, "txt_shingle", s, "html", f, group="txt_page")
    for h in soup.find_all(["h1", "h2"])[:6]:
        ht = normalise_text(h.get_text(" "))
        if len(ht) > 12:
            g.add(Family.TXT, "headline", ht, "html", f, group="txt_page")
    for ph in set(_PHONE.findall(body)):
        g.add(Family.TXT, "entity_phone", re.sub(r"\D", "", ph)[-9:], "html", f)
    for em in set(_EMAIL.findall(body)):
        g.add(Family.TXT, "entity_email", em.lower(), "html", f)
    # DOM skeleton: 4-grams of tag+class paths (page kit fingerprint, independent of the words)
    tags = [f"{t.name}.{'.'.join(sorted(t.get('class', [])))[:40]}" for t in soup.find_all(True)]
    for i in range(max(0, len(tags) - 3)):
        sh = hashlib.blake2b("|".join(tags[i:i + 4]).encode(), digest_size=8).hexdigest()
        g.add(Family.INF, "dom_shingle", sh, "html", f, group="inf_dom")
    for a in soup.find_all(["a", "form", "iframe", "img", "link"], limit=500):
        u = a.get("href") or a.get("action") or a.get("src") or ""
        host = (urlsplit(u).hostname or "").lower() if u.startswith("http") else ""
        if host and not is_boilerplate(host):
            g.add(Family.INF, "link_host", host, "html", f, group="inf_host")
    low = normalise_text(title + " " + body[:20000])
    for scheme, words in SCHEMES.items():
        hits = sum(low.count(w) for w in words)
        if hits >= 2:
            g.add(Family.NAR, "scheme", scheme, "keywords", f, prov=Provenance.INF)


def _image_phash(path: Path) -> str | None:
    import imagehash
    from PIL import Image

    with Image.open(path) as im:
        if im.width < 48 or im.height < 48:
            return None
        return str(imagehash.phash(im.convert("RGB")))


def _image(g: _G, f: FileEntry, landing: bool) -> None:
    fam_sha = Family.INF if landing else Family.VIS
    g.add(fam_sha, "asset_sha256", f.sha256, "sha256", f, group="asset" if landing else "vis_img")
    try:
        ph = _image_phash(f.path)
    except Exception:  # noqa: BLE001 — svg/corrupt: no visual gene, not an error for the incident
        ph = None
    if ph:
        g.add(Family.VIS, "phash", ph, "phash", f, group="vis_img", quality=0.7 if landing else 1.0)


def _video(g: _G, f: FileEntry) -> None:
    import cv2
    import imagehash
    from PIL import Image

    g.add(Family.VIS, "asset_sha256", f.sha256, "sha256", f, group="vis_img")
    cap = cv2.VideoCapture(str(f.path))
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
            if n <= 0:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * frac))
            ok, frame = cap.read()
            if not ok:
                continue
            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            g.add(Family.VIS, "phash", str(imagehash.phash(im)), "video_frame", f, group="vis_img",
                  timecode_s=round(n * frac / fps, 2), quality=0.8)
    finally:
        cap.release()


def _info(g: _G, f: FileEntry) -> None:
    d = load_info_json(f.path)
    for key, kind in (("uploader_id", "uploader"), ("uploader", "uploader"), ("channel_id", "channel"),
                      ("id", "platform_ad_id"), ("extractor_key", "platform")):
        v = d.get(key)
        if v:
            g.add(Family.META, kind, str(v).lower(), "info_json", f)
    for key in ("title", "description"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            g.add(Family.TXT, "meta_text", normalise_text(v)[:300], "info_json", f, group="txt_meta")
            for s in shingles(v, n=3):
                g.add(Family.TXT, "txt_shingle", s, "info_json", f, group="txt_page")
    url = d.get("webpage_url") or d.get("original_url")
    if isinstance(url, str):
        host = (urlsplit(url).hostname or "").lower()
        if host and not is_boilerplate(host):
            g.add(Family.INF, "host", host, "info_json", f, group="inf_host")


def extract(inc: IncidentFiles) -> list[Gene]:
    g = _G(inc.incident_id)
    handlers = {
        str(ArtifactRole.HAR): _har,
        str(ArtifactRole.LANDING_HTML): _html,
        str(ArtifactRole.PLATFORM_METADATA): _info,
    }
    for f in inc.files:
        role = str(f.role)
        try:
            if role in handlers:
                handlers[role](g, f)
            elif role in {str(ArtifactRole.AD_VIDEO), str(ArtifactRole.SCREEN_RECORDING)}:
                _video(g, f)
            elif f.mime.startswith("image/"):
                _image(g, f, landing=role == str(ArtifactRole.LANDING_ASSET))
            elif role == str(ArtifactRole.LANDING_ASSET):
                g.add(Family.INF, "asset_sha256", f.sha256, "sha256", f, group="asset")
        except Exception as exc:  # noqa: BLE001 — record, never crash the incident
            g.status(Family.META, f"extract_{role}", GeneStatus.FAILED, f"{f.rel}: {type(exc).__name__}: {exc}"[:200])
    observed = {str(x.family) for x in g.genes if str(x.status) == GeneStatus.OBSERVED}
    for fam in Family:
        if str(fam) not in observed:
            st = GeneStatus.NOT_APPLICABLE if fam == Family.ID else GeneStatus.UNAVAILABLE
            g.status(fam, "family", st, "no observation in this incident (stub extractor)")
    return g.genes
