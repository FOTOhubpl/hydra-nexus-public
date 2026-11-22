"""Collect every piece of text an incident carries, with exact provenance (shared by TXT, NAR, META).

Sources (by ArtifactRole):
- ad creatives, feed screenshots, landing screenshots, landing images (`_files/`): OCR lines with boxes;
  phone screenshots are split into zones (ad / feed noise / Ad Library panel / Ad Library ad copy);
- ad videos and screen recordings: OCR on sampled frames (hydra.media), consecutive duplicates dropped;
- landing HTML / MHTML: title, meta, headline (h1), lead, body, navigation, comments, form texts, alt texts;
- platform metadata (.info.json): title / description (the ad text when present);
- text / chat logs: one segment per line;
- speech in ad videos / screen recordings / audio files: ASR transcript segments (source `asr`, channel `ad`,
  evidence timecode), see hydra.genome.aud.

`collect(inc, ctx)` is memoised per incident, so TXT, NAR and META extractors share one OCR pass.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from hydra.contracts import ArtifactRole, EvidencePointer
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident
from hydra.genome.ocr_pool import OcrBox, OcrPool, OcrResult, is_phone_screenshot, shared_pool
from hydra.genome.txt_entities import ocr_fix
from hydra.genome.txt_norm import fold, light

log = logging.getLogger(__name__)

IMAGE_ROLES = (ArtifactRole.AD_CREATIVE, ArtifactRole.FEED_SCREENSHOT, ArtifactRole.LANDING_SCREENSHOT)
VIDEO_ROLES = (ArtifactRole.AD_VIDEO, ArtifactRole.SCREEN_RECORDING)
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
MAX_ASSET_IMAGES = 30
MIN_ASSET_SIDE = 180


@dataclass
class Segment:
    text: str                       # light-normalised
    raw: str                        # as observed (OCR output / DOM text)
    evidence: EvidencePointer
    quality: float = 1.0
    zone: str = "main"              # ad | feed | library_panel | library_ad | main
    chrome: bool = False            # platform UI text (Sponsorowane, Dowiedz się więcej, counters)
    height: float = 0.0             # OCR glyph height in px (headline heuristic)
    meta: dict = field(default_factory=dict)


@dataclass
class TextBlock:
    channel: str      # ad | page | asset | meta | text
    source: str       # ocr_image | ocr_frame | html_title | html_meta | html_headline | html_lead | html_body |
    #                   html_nav | html_comment | html_form | html_alt | html_byline | meta_title |
    #                   meta_description | plain_text | asr
    artifact_id: str
    segments: list[Segment] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text)


@dataclass
class IncidentTexts:
    blocks: list[TextBlock] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # (artifact_id, message)
    # (artifact_id, message) of sources whose engine/weights are not installed: unknown, not a failure (CR-101)
    unavailable: list[tuple[str, str]] = field(default_factory=list)
    ocr_seconds: float = 0.0
    ocr_images: int = 0
    ocr_cached: int = 0
    attempted: list[str] = field(default_factory=list)  # artifact ids that had a text source

    def by_source(self, *sources: str) -> list[TextBlock]:
        return [b for b in self.blocks if b.source in sources]

    def by_channel(self, *channels: str) -> list[TextBlock]:
        return [b for b in self.blocks if b.channel in channels]


# ----------------------------------------------------------------------------- OCR zones

_CHROME = re.compile(
    r"^(sponsorowane|sponsored|reklama|dowiedz sie wiecej|learn more|zobacz tlumaczenie|see translation|"
    r"wiecej|more|obserwuj|follow|lubie to|like|komentarz|comment|udostepnij|share|wyslij|send|"
    r"zapisz|save|odpowiedz|reply|zobacz wiecej|see more|\d+ (wyswietlen\w*|polubien\w*|komentarz\w*)|"
    r"zagraj w gre|play game|pobierz aplikacje|install now|zainstaluj|wyswietl profil|"
    r"[a-z0-9]{1,2}|[\d\s.,:kmKM%]+|instagram|facebook|www facebook com|messenger)$"
)
_LIBRARY = re.compile(r"identyfikator biblioteki|library id|data zamieszczenia|started running|transparentnosc w ue")
_LIB_END = re.compile(r"niska liczba wyswietlen|zamknij|close|zobacz szczegoly reklamy|wyswietlenia|"
                      r"see ad details|low impression")
_USER = re.compile(r"^[a-z0-9][a-z0-9._]{2,29}$")


def _row_key(b: OcrBox) -> tuple[int, int]:
    return (b.box[1] + b.box[3]) // 2, b.box[0]


def zone_lines(lines: list[OcrBox], width: int, height: int = 0) -> list[tuple[OcrBox, str, bool]]:
    """Assign each OCR line to a zone and flag platform chrome. Lines sorted top-to-bottom.

    Feed segmentation (ad block = advertiser line + "Sponsorowane" ... up to the next post header) runs only
    on portrait phone screenshots; desktop crops and creatives are the ad itself."""
    lines = sorted(lines, key=_row_key)
    folded = [" ".join(fold(b.text).split()) for b in lines]
    chrome = [bool(_CHROME.match(f)) or len(f) <= 2 for f in folded]
    zones = ["ad"] * len(lines)
    lib_idx = [i for i, f in enumerate(folded) if _LIBRARY.search(f)]
    spons = [i for i, f in enumerate(folded) if f in ("sponsorowane", "sponsored")]
    if lib_idx:
        zones = ["library_panel"] * len(lines)
        for s in spons:
            if s < lib_idx[0]:
                continue
            for j in range(s + 1, len(lines)):
                if _LIB_END.search(folded[j]) or _LIBRARY.search(folded[j]):
                    break
                zones[j] = "library_ad"
            if s > 0:
                zones[s - 1] = "library_ad"  # page name above "Sponsorowane"
        return [(b, z, c) for b, z, c in zip(lines, zones, chrome, strict=True)]
    if not spons or not is_phone_screenshot(width, height):
        return [(b, "ad", c) for b, c in zip(lines, chrome, strict=True)]
    zones = ["feed"] * len(lines)
    for s in spons:
        start = s - 1 if s > 0 and lines[s].box[1] - lines[s - 1].box[3] < 3 * max(1, lines[s].box[3] -
                                                                                    lines[s].box[1]) else s
        end = len(lines)
        for j in range(s + 1, len(lines)):
            f = folded[j]
            row_mates = [k for k in range(len(lines)) if k != j and abs(_row_key(lines[k])[0] -
                                                                          _row_key(lines[j])[0]) < 25]
            follows = any(folded[k] in ("obserwuj", "follow") for k in row_mates)
            if (_USER.match(f) and "." in lines[j].text or follows) and lines[j].box[0] < 0.4 * width and \
                    f not in ("sponsorowane",):
                end = j
                break
            if j in spons:
                end = j - 1
                break
        for j in range(start, end):
            zones[j] = "ad"
    return [(b, z, c) for b, z, c in zip(lines, zones, chrome, strict=True)]


def ocr_segments(res: OcrResult, artifact_id: str, timecode: float | None = None) -> list[Segment]:
    segs = []
    for b, zone, chrome in zone_lines(res.lines, res.size[0], res.size[1]):
        x0, y0, x1, y1 = b.box
        ev = EvidencePointer(artifact_id=artifact_id, bbox=(float(x0), float(y0), float(x1 - x0), float(y1 - y0)),
                             timecode_s=timecode)
        segs.append(Segment(light(ocr_fix(b.text)), b.text, ev, quality=float(b.score), zone=zone, chrome=chrome,
                            height=float(y1 - y0)))
    return segs


# ----------------------------------------------------------------------------- HTML

_DROP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object", "embed", "canvas", "head",
              "link", "meta"}
_BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "figcaption", "dt", "dd",
          "div", "section", "article", "label", "button", "header", "footer", "nav", "aside", "main", "ul", "ol",
          "table", "tr", "form", "figure", "details", "summary", "option", "select", "textarea"}
_NAV_RX = re.compile(r"(^|[-_ ])(nav|navbar|menu|header|footer|breadcrumb|topbar|masthead)([-_ ]|$)", re.I)
_COMMENT_RX = re.compile(r"comment", re.I)
_HIDE_RX = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_GOOG_RX = re.compile(r"^(goog-|skiptranslate)", re.I)
_LEAD_RX = re.compile(r"(^|[-_ ])(lead|intro|subtitle|excerpt|standfirst|summary|subheadline|deck|perex)"
                      r"([-_ ]|$)", re.I)
_BYLINE_RX = re.compile(r"(^|[-_ ])(author|byline|autor)([-_ ]|$)", re.I)
_NAME_RX = re.compile(r"(^|[-_ ])(name|author|user|nick)([-_ ]|$)", re.I)
_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_-]+)", re.I)


def _attrs(el) -> str:
    return f"{el.get('class') or ''} {el.get('id') or ''}"


def read_html(path: Path) -> str:
    raw = path.read_bytes()
    head = raw[:4096].lstrip()
    if path.suffix.lower() in (".mhtml", ".mht") or head.startswith(b"From:") or b"MIME-Version:" in head[:400]:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_content()
                except Exception:  # noqa: BLE001
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    m = _CHARSET.search(raw[:4096])
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _text_of(el) -> str:
    return " ".join("".join(el.itertext()).split())


def html_blocks(html: str, artifact_id: str) -> list[TextBlock]:
    import lxml.etree
    import lxml.html

    html = re.sub(r"^\s*<\?xml[^>]*>", "", html)
    if not html.strip():
        return []
    root = lxml.html.document_fromstring(html)
    tree = lxml.etree.ElementTree(root)
    out: list[TextBlock] = []

    def seg(text: str, el, src: str, **meta) -> Segment:
        return Segment(light(text), text, EvidencePointer(artifact_id=artifact_id,
                                                          dom_path=tree.getpath(el) if el is not None else None,
                                                          note=src), meta=meta)

    # head: title + meta
    title_el = root.find(".//title")
    if title_el is not None and _text_of(title_el):
        out.append(TextBlock("page", "html_title", artifact_id, [seg(_text_of(title_el), title_el, "title")]))
    metas = []
    for m in root.iter("meta"):
        key = (m.get("property") or m.get("name") or "").lower()
        if key in ("description", "og:title", "og:description", "og:site_name", "twitter:title",
                   "twitter:description", "author", "keywords") and (m.get("content") or "").strip():
            metas.append(seg(m.get("content").strip(), m, key, key=key))
    if metas:
        out.append(TextBlock("page", "html_meta", artifact_id, metas))
    declared_lang = root.get("lang")

    # drop invisible / non-content
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        a = _attrs(el)
        if el.tag in _DROP_TAGS or el.get("hidden") is not None or _HIDE_RX.search(el.get("style") or "") \
                or _GOOG_RX.search((el.get("class") or "").strip()) or _GOOG_RX.search(el.get("id") or "") \
                or "skiptranslate" in a:
            if el.getparent() is not None:
                el.drop_tree()  # keeps the tail text
    body = root.find(".//body")
    if body is None:
        body = root

    # alt texts before removing anything else
    alts = [seg(i.get("alt").strip(), i, "img alt") for i in body.iter("img") if (i.get("alt") or "").strip()
            and len(i.get("alt").strip()) > 3]

    # comments (outermost containers)
    comment_roots = []
    for el in body.iter():
        if isinstance(el.tag, str) and _COMMENT_RX.search(_attrs(el)) and \
                not any(_COMMENT_RX.search(_attrs(p)) for p in el.iterancestors() if isinstance(p.tag, str)):
            comment_roots.append(el)
    comment_segs, names = [], []
    for cr in comment_roots:
        for el in cr.iter():
            if isinstance(el.tag, str) and _NAME_RX.search(_attrs(el)) and _text_of(el) and len(_text_of(el)) < 60:
                names.append(_text_of(el))
        items = [c for c in cr if isinstance(c.tag, str) and _text_of(c)] or [cr]
        for it in items:
            t = _text_of(it)
            if t:
                comment_segs.append(seg(t, it, "comment"))
    # nav / header / footer containers
    nav_roots = [el for el in body.iter() if isinstance(el.tag, str) and el not in comment_roots and
                 (el.tag in ("nav", "header", "footer") or el.get("role") == "navigation" or
                  _NAV_RX.search(_attrs(el))) and
                 not any(p.tag in ("nav", "header", "footer") or _NAV_RX.search(_attrs(p))
                         for p in el.iterancestors() if isinstance(p.tag, str))]
    # never treat a container holding the h1 + most of the text as navigation
    total_len = len(_text_of(body)) or 1
    nav_roots = [n for n in nav_roots if len(_text_of(n)) < 0.3 * total_len and n.find(".//h1") is None]
    nav_segs = [seg(_text_of(n), n, "nav") for n in nav_roots if _text_of(n)]
    forms = [f for f in body.iter("form")]
    form_segs = []
    for f in forms:
        parts = [_text_of(f)]
        for i in f.iter("input", "textarea", "button"):
            for attr in ("placeholder", "value", "aria-label", "title"):
                v = (i.get(attr) or "").strip()
                if v and (attr != "value" or i.get("type") in ("submit", "button")):
                    parts.append(v)
        t = " ".join(p for p in parts if p)
        if t.strip():
            form_segs.append(seg(t, f, "form"))
    for i in body.iter("input", "textarea"):  # placeholders outside forms (SDK-injected forms)
        if i.getparent() is not None and not any(p.tag == "form" for p in i.iterancestors()):
            v = (i.get("placeholder") or "").strip()
            if v:
                form_segs.append(seg(v, i, "placeholder"))

    for el in comment_roots + nav_roots + forms:
        if el.getparent() is not None:
            el.drop_tree()

    # bylines
    bylines = [seg(_text_of(el), el, "byline") for el in body.iter() if isinstance(el.tag, str) and
               _BYLINE_RX.search(_attrs(el)) and 2 < len(_text_of(el)) < 80]

    # headline + lead + body leaf blocks
    h1 = body.find(".//h1")
    headline_segs = [seg(_text_of(h1), h1, "h1")] if h1 is not None and _text_of(h1) else []
    lead_segs = []
    for el in body.iter():
        if isinstance(el.tag, str) and _LEAD_RX.search(_attrs(el)) and len(_text_of(el)) >= 40:
            lead_segs.append(seg(_text_of(el), el, "lead"))
            break
    body_segs: list[Segment] = []
    for el in body.iter():
        if not isinstance(el.tag, str) or el.tag not in _BLOCK:
            continue
        has_block_child = any(isinstance(c.tag, str) and c.tag in _BLOCK for c in el.iterdescendants())
        if not has_block_child:
            t = _text_of(el)
            if t:
                body_segs.append(seg(t, el, el.tag))
        else:
            own = " ".join(x.strip() for x in [el.text or ""] + [c.tail or "" for c in el] if x and x.strip())
            if len(own) > 20:
                body_segs.append(seg(own, el, el.tag + " own-text"))
    # body may live outside block tags (spans directly in body)
    if not body_segs and _text_of(body):
        body_segs.append(seg(_text_of(body), body, "body"))
    if not lead_segs and h1 is not None:
        seen_h1 = False
        for s in body_segs:
            if s.evidence.dom_path == tree.getpath(h1):
                seen_h1 = True
                continue
            if seen_h1 and len(s.text) >= 60:
                lead_segs = [Segment(s.text, s.raw, s.evidence.model_copy(update={"note": "lead (first ≥60 chars)"}),
                                     quality=0.7)]
                break
    for src, segs in (("html_headline", headline_segs), ("html_lead", lead_segs), ("html_body", body_segs),
                      ("html_byline", bylines), ("html_nav", nav_segs), ("html_comment", comment_segs),
                      ("html_form", form_segs), ("html_alt", alts)):
        if segs:
            out.append(TextBlock("page", src, artifact_id, segs, meta={"lang": declared_lang} if src ==
                                 "html_body" else {}))
    if names:
        out.append(TextBlock("page", "html_commenter", artifact_id,
                             [Segment(light(n), n, EvidencePointer(artifact_id=artifact_id, note="commenter"),
                                      quality=0.5) for n in dict.fromkeys(names)]))
    if declared_lang:
        for b in out:
            b.meta.setdefault("declared_lang", declared_lang)
    return out


# ----------------------------------------------------------------------------- metadata / text files


def metadata_blocks(path: Path, artifact_id: str) -> list[TextBlock]:
    try:
        d = json.loads(path.read_text("utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(d, dict):
        return []
    out = []
    for key, src in (("title", "meta_title"), ("fulltitle", "meta_title"), ("description", "meta_description"),
                     ("playlist_title", "meta_title")):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            out.append(TextBlock("ad", src, artifact_id, [Segment(light(v), v, EvidencePointer(
                artifact_id=artifact_id, note=f"json:{key}"))]))
    return out


def _json_strings(obj, out: list[str], depth: int = 0) -> None:
    if depth > 20 or len(out) > 5000:
        return
    if isinstance(obj, str):
        if len(obj.strip()) >= 3:
            out.append(obj.strip())
    elif isinstance(obj, dict):
        for v in obj.values():
            _json_strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _json_strings(v, out, depth + 1)


def svg_blocks(path: Path, artifact_id: str) -> list[TextBlock]:
    """Text of an SVG (`<text>`, `<title>`, `<desc>`), parsed without entities, DTDs or network; scripts and event
    handlers are dropped, never executed."""
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
                             remove_comments=True, remove_pis=True)
    try:
        with path.open("rb") as fh:
            root = etree.fromstring(fh.read(5_000_000), parser)
    except (etree.XMLSyntaxError, ValueError, OSError):
        return []
    segs = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
        if tag in ("text", "tspan", "title", "desc", "textPath"):
            s = " ".join((el.text or "").split())
            if s:
                segs.append(Segment(light(s), s, EvidencePointer(artifact_id=artifact_id, note=f"svg <{tag}>")))
    return [TextBlock("ad", "svg_text", artifact_id, segs)] if segs else []


def text_file_blocks(path: Path, artifact_id: str, as_json: bool | None = None) -> list[TextBlock]:
    """Text / chat log / PDF → one `plain_text` block. Chat exports (hydra.genome.chat) give one segment per
    message body (sender and timestamp kept in the evidence note, not in the text); PDFs give their text lines
    and link URIs; anything else one segment per line (JSON: its string values)."""
    from hydra.genome.chat import parse_chat, pdf_text, read_text_bytes

    with path.open("rb") as fh:
        head = fh.read(5_000_000)
    if head.startswith(b"%PDF"):
        lines, uris = pdf_text(path)
        segs = [Segment(light(ln), ln, EvidencePointer(artifact_id=artifact_id, note=f"pdf line {i}"))
                for i, ln in enumerate([*lines, *uris]) if ln.strip()]
        return [TextBlock("text", "plain_text", artifact_id, segs, meta={"format": "pdf"})] if segs else []
    chat = parse_chat(path, as_json=as_json)
    if chat is not None:
        segs = []
        for m in chat.messages:
            for ln in m.text.splitlines():
                if ln.strip():
                    segs.append(Segment(light(ln), ln, EvidencePointer(
                        artifact_id=artifact_id, note=f"{chat.platform} message {m.index} {m.ts or ''} "
                                                      f"{m.sender or ''}".strip()[:200]), meta={"sender": m.sender}))
        return [TextBlock("text", "plain_text", artifact_id, segs, meta={"format": chat.platform})] if segs else []
    raw = read_text_bytes(head)
    lines: list[str] = []
    if as_json or (as_json is None and path.suffix.lower() == ".json"):
        try:
            _json_strings(json.loads(raw), lines)
        except json.JSONDecodeError:
            lines = raw.splitlines()
    else:
        lines = raw.splitlines()
    segs = []
    pos = 0
    for ln in lines:
        if ln.strip():
            segs.append(Segment(light(ln), ln, EvidencePointer(artifact_id=artifact_id, char_range=(pos, pos + len(ln)),
                                                               note="line")))
        pos += len(ln) + 1
    return [TextBlock("text", "plain_text", artifact_id, segs)] if segs else []


# ----------------------------------------------------------------------------- video frames


def video_frames(art: ResolvedArtifact, ctx: ExtractContext, max_frames: int = 48) -> list[tuple[Path, float]]:
    from hydra.media import extract_frames, probe

    dur = 0.0
    try:
        info = probe(art.path)
        dur = float(info.get("format", {}).get("duration") or 0.0)
    except Exception:  # noqa: BLE001
        dur = float(art.meta.meta.get("duration") or 0.0)
    every = max(1.0, round(dur / max_frames, 2)) if dur else 2.0
    out_dir = ctx.work_dir / "txt_frames" / art.meta.artifact_id[:16] / f"e{every}"
    frames = sorted(out_dir.glob("frame_*.jpg")) if out_dir.is_dir() else []
    if not frames:
        frames = extract_frames(art.path, out_dir, every_s=every, max_side=1280, max_frames=max_frames)
    # frame_000001.jpg ~ t=0 s (hydra.media docstring)
    return [(f, round((int(f.stem.split("_")[1]) - 1) * every, 2)) for f in frames]


# ----------------------------------------------------------------------------- collection


def _pool(ctx: ExtractContext) -> OcrPool:
    pool = ctx.models.get("ocr_pool")
    if pool is None:
        pool = shared_pool(ctx.settings.cache_dir, ctx.options.get("ocr_engine", "rapidocr"),
                           ctx.options.get("ocr_workers"))
        ctx.models["ocr_pool"] = pool
    return pool


def _is_html(a: ResolvedArtifact) -> bool:
    return a.path.suffix.lower() in (".html", ".htm", ".mhtml", ".mht", ".xhtml") or "html" in a.meta.mime \
        or a.meta.mime == "multipart/related" or str(a.meta.meta.get("format", "")).startswith(("html", "mhtml",
                                                                                                "rendered"))


def is_json(a: ResolvedArtifact) -> bool:
    """CAS paths have no suffix: decide by name, MIME or adapter meta."""
    return a.path.suffix.lower() == ".json" or a.meta.path_in_incident.lower().endswith(".json") or \
        "json" in a.meta.mime or a.meta.meta.get("kind") in ("ytdlp_info", "capture_manifest")


_MEMO: OrderedDict[str, IncidentTexts] = OrderedDict()
_MEMO_MAX = 64


def _memo_key(inc: ResolvedIncident, ctx: ExtractContext) -> str:
    h = hashlib.sha256(inc.incident.id.encode())
    for a in inc.artifacts:
        h.update(f"|{a.meta.artifact_id}:{a.meta.role}".encode())
    # Options that `_collect` cannot read stay out of the key: `incident_genes` (the dependent NAR input, hundreds
    # of pydantic genes whose repr took a third of the parent's time, SCALE 2026-11-21) and `_`-prefixed plumbing
    # (per-incident caches, worker module lists).
    h.update(json.dumps({k: str(v) for k, v in sorted(ctx.options.items())
                         if k != "incident_genes" and not k.startswith("_")}).encode())
    return h.hexdigest()


def collect(inc: ResolvedIncident, ctx: ExtractContext) -> IncidentTexts:
    key = _memo_key(inc, ctx)
    if key in _MEMO:
        _MEMO.move_to_end(key)
        return _MEMO[key]
    res = _collect(inc, ctx)
    _MEMO[key] = res
    while len(_MEMO) > _MEMO_MAX:
        _MEMO.popitem(last=False)
    return res


def _collect(inc: ResolvedIncident, ctx: ExtractContext) -> IncidentTexts:
    out = IncidentTexts()
    pool = _pool(ctx)
    # ---- images (one pool batch per incident)
    image_jobs: list[tuple[ResolvedArtifact, str]] = []
    for a in inc.by_role(*IMAGE_ROLES):
        if a.meta.mime.startswith("image/svg") or a.path.suffix.lower() == ".svg":
            out.attempted.append(a.meta.artifact_id)  # vector image: its text nodes, never rendered or run
            out.blocks.extend(svg_blocks(a.path, a.meta.artifact_id))
            continue
        image_jobs.append((a, "ad" if a.meta.role != ArtifactRole.LANDING_SCREENSHOT else "page"))
    if ctx.options.get("ocr_assets", True):
        assets = [a for a in inc.by_role(ArtifactRole.LANDING_ASSET) if a.path.suffix.lower() in IMAGE_EXT
                  or a.meta.mime.startswith("image/")]
        assets = [a for a in assets if not a.meta.mime.endswith("svg+xml") and a.path.suffix.lower() != ".svg"]
        big = []
        for a in assets:
            w, h = a.meta.meta.get("width"), a.meta.meta.get("height")
            if w is None or h is None:
                try:
                    from PIL import Image

                    with Image.open(a.path) as im:
                        w, h = im.size
                except Exception:  # noqa: BLE001
                    continue
            if min(w, h) >= MIN_ASSET_SIDE:
                big.append((w * h, a))
        big.sort(key=lambda t: -t[0])
        image_jobs += [(a, "asset") for _, a in big[:MAX_ASSET_IMAGES]]
    # ---- video frames
    frame_jobs: list[tuple[ResolvedArtifact, Path, float]] = []
    for a in inc.by_role(*VIDEO_ROLES):
        out.attempted.append(a.meta.artifact_id)
        try:
            for fp, tc in video_frames(a, ctx, int(ctx.options.get("ocr_max_frames", 48))):
                frame_jobs.append((a, fp, tc))
        except Exception as exc:  # noqa: BLE001
            out.errors.append((a.meta.artifact_id, f"frames: {type(exc).__name__}: {exc}"[:300]))
    batch = [(a.path, a.meta.artifact_id) for a, _ in image_jobs] + [(fp, None) for _, fp, _ in frame_jobs]
    results = pool.run(batch) if batch else []
    for r in results:
        out.ocr_images += 1
        out.ocr_cached += int(r.cached)
        out.ocr_seconds += 0.0 if r.cached else r.seconds
    for (a, channel), r in zip(image_jobs, results[: len(image_jobs)], strict=True):
        out.attempted.append(a.meta.artifact_id)
        if not r.ok:
            dest = out.unavailable if r.error_kind == "unavailable" else out.errors
            dest.append((a.meta.artifact_id, f"ocr: {r.error}"))
            continue
        segs = [s for s in ocr_segments(r, a.meta.artifact_id) if s.text]
        if segs:
            out.blocks.append(TextBlock(channel, "ocr_image", a.meta.artifact_id, segs,
                                        meta={"size": r.size, "crop": r.crop, "role": a.meta.role}))
    # frames: keep a frame's lines only when its text differs from the previous frame of the same video
    prev: dict[str, str] = {}
    per_video: dict[str, list[Segment]] = {}
    for (a, _fp, tc), r in zip(frame_jobs, results[len(image_jobs):], strict=True):
        if not r.ok:
            continue
        segs = [s for s in ocr_segments(r, a.meta.artifact_id, timecode=tc) if s.text]
        sig = " ".join(fold(s.text) for s in segs if not s.chrome)
        sig = " ".join(sig.split())
        if not sig or sig == prev.get(a.meta.artifact_id):
            continue
        prev[a.meta.artifact_id] = sig
        per_video.setdefault(a.meta.artifact_id, []).extend(segs)
    for aid, segs in per_video.items():
        out.blocks.append(TextBlock("ad", "ocr_frame", aid, segs))
    # ---- speech (ASR transcripts of video/audio, hydra.genome.aud; unavailable offline without weights)
    if ctx.options.get("asr", True):
        from hydra.genome.aud import asr_blocks

        out.blocks.extend(asr_blocks(inc, ctx, out.errors))
    # ---- HTML
    for a in inc.by_role(ArtifactRole.LANDING_HTML):
        if not _is_html(a):
            continue
        out.attempted.append(a.meta.artifact_id)
        try:
            out.blocks.extend(html_blocks(read_html(a.path), a.meta.artifact_id))
        except Exception as exc:  # noqa: BLE001
            out.errors.append((a.meta.artifact_id, f"html: {type(exc).__name__}: {exc}"[:300]))
    # ---- platform metadata + text
    for a in inc.by_role(ArtifactRole.PLATFORM_METADATA):
        if is_json(a):
            out.attempted.append(a.meta.artifact_id)
            out.blocks.extend(metadata_blocks(a.path, a.meta.artifact_id))
    for a in inc.by_role(ArtifactRole.TEXT):
        out.attempted.append(a.meta.artifact_id)
        try:
            out.blocks.extend(text_file_blocks(a.path, a.meta.artifact_id, as_json=is_json(a)))
        except Exception as exc:  # noqa: BLE001
            out.errors.append((a.meta.artifact_id, f"text: {type(exc).__name__}: {exc}"[:300]))
    _dedupe(out)
    return out


def _dedupe(out: IncidentTexts) -> None:
    """Same page saved twice (dom.html + page.mhtml, or two identical saves): keep the first block copy."""
    seen: set[tuple[str, str]] = set()
    kept = []
    for b in out.blocks:
        if b.source.startswith("ocr"):
            kept.append(b)
            continue
        key = (b.source, hashlib.sha1(b.text.encode()).hexdigest())
        if key in seen:
            continue
        seen.add(key)
        kept.append(b)
    out.blocks = kept
