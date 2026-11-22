"""INF genes from the saved landing page: DOM, scripts, assets, forms, comments, brand markers, contact tokens.

One extractor `inf_dom` (static parsing with BeautifulSoup/lxml; scripts are hashed and regex-scanned, never run).
Sources: landing_html artifacts (HTML / MHTML / rendered DOM), landing_asset artifacts (`_files/`, capture
`resources/`) and HTML/JS/CSS bodies stored in HAR files.

Kit-level genes (dependency_group "inf:kit:*", used by CORE for campaign groups): css_build_hash, js_build_hash,
lead_form_kit. Template genes: dom_shingle (bottom-k MinHash-ready values), dom_skeleton_hash, dom_token.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

from hydra.contracts import Family, GeneStatus
from hydra.enrich.domains import is_boilerplate, valid_host
from hydra.genome import adv_f01, parked
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.fuzzy import MIN_BYTES as MIN_TLSH_BYTES
from hydra.genome.fuzzy import tlsh_value
from hydra.genome.har import _emit_scan
from hydra.genome.kitset import BAND_GROUP, BAND_KIND, SKIP_NAME, FileSet, file_set_key, page_folder
from hydra.genome.kitset import emit as emit_fileset
from hydra.genome.lure import emit_brand_offdomain
from hydra.genome.operator_ids import find_accounts, find_messenger_handles, find_wallets, phone_profile
from hydra.genome.webparse import (
    EMAIL_RX,
    FILE_TOO_LARGE,
    LIB_NAME,
    MAX_TEXT_SCAN,
    PHONE_RX,
    GeneSink,
    HtmlParsed,
    Scan,
    bundle,
    favicon_hash,
    h16,
    norm_js,
    parse_html_bytes,
    pii_hash,
    scan_text,
)

SHINGLE_K = 4  # consecutive tag paths per shingle
SHINGLE_KEEP = 64  # bottom-k sketch size per page (MinHash-ready: Jaccard estimate from shared values)
MIN_ASSET = 512

# Impersonated media / institution brands (PL market + global). Matched in title, og:site_name, masthead
# (header/nav/footer/logo) text and asset file names. Value = canonical brand id.
BRANDS: dict[str, re.Pattern[str]] = {
    "gazeta.pl": re.compile(r"gazeta\.pl|logo-gazeta|\bgazeta wyborcza\b|wyborcza", re.I),
    "tvn24": re.compile(r"\btvn\s?24\b|tvn24", re.I),
    "tvn": re.compile(r"\btvn\b(?!\s?24)", re.I),
    "onet": re.compile(r"\bonet(\.pl)?\b", re.I),
    "wp.pl": re.compile(r"\bwp\.pl\b|wirtualna polska", re.I),
    "interia": re.compile(r"\binteria\b", re.I),
    "polsat": re.compile(r"\bpolsat\b", re.I),
    "tvp": re.compile(r"\btvp(\s?info)?\b|tvp\.info", re.I),
    "rmf": re.compile(r"\brmf\s?(fm|24)\b", re.I),
    "radio_zet": re.compile(r"radio\s?zet", re.I),
    "fakt": re.compile(r"\bfakt(\.pl|24)?\b", re.I),
    "super_express": re.compile(r"super\s?express|se\.pl", re.I),
    "rzeczpospolita": re.compile(r"rzeczpospolita|\brp\.pl\b", re.I),
    "money.pl": re.compile(r"money\.pl", re.I),
    "bankier": re.compile(r"\bbankier(\.pl)?\b", re.I),
    "business_insider": re.compile(r"business\s?insider", re.I),
    "forbes": re.compile(r"\bforbes\b", re.I),
    "newsweek": re.compile(r"\bnewsweek\b", re.I),
    "o2": re.compile(r"\bo2\.pl\b", re.I),
    "pudelek": re.compile(r"\bpudelek\b", re.I),
    "natemat": re.compile(r"natemat", re.I),
    "bbc": re.compile(r"\bbbc\b", re.I),
    "cnn": re.compile(r"\bcnn\b", re.I),
    "reuters": re.compile(r"\breuters\b", re.I),
    "app_store": re.compile(r"app store|apps\.apple\.com", re.I),
    "google_play": re.compile(r"google play|play\.google\.com", re.I),
    "pekao": re.compile(r"\bpekao\b", re.I),
    "pko": re.compile(r"\bpko\s?(bp)?\b", re.I),
    "mbank": re.compile(r"\bmbank\b", re.I),
    "nbp": re.compile(r"\bnbp\b|narodowy bank polski", re.I),
    "gov.pl": re.compile(r"\bgov\.pl\b|ministerstw|rząd rp", re.I),
    "inpost": re.compile(r"\binpost\b", re.I),
    "orlen": re.compile(r"\borlen\b", re.I),
    "baltic_pipe": re.compile(r"baltic pipe", re.I),
}


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"\d+", "0", s)
    return re.sub(r"\W+", " ", s).strip()


def _bottom_k(items: set[str], k: int) -> list[str]:
    return sorted(items)[:k]


def _shingles(paths: list[str]) -> set[str]:
    return {h16("|".join(paths[i : i + SHINGLE_K])) for i in range(max(0, len(paths) - SHINGLE_K + 1))}


def _clean_name(name: str) -> str:
    n = re.sub(r"\.(pobrane|pobierz|download)$", "", name, flags=re.I)
    n = re.sub(r"\(\d+\)", "", n)
    n = re.sub(r"_[0-9a-f]{8}(\.[a-z0-9]+)$", r"\1", n)  # page-capture resources/<name>_<sha8>.<ext>
    return n.strip().lower()


def _is_lib(name: str, host: str | None) -> bool:
    """Library, CMS core or CDN helper file (G06: shared byte-for-byte by unrelated sites), or a boilerplate host."""
    return bool(LIB_NAME.match(name) or SKIP_NAME.match(name)) or (bool(host) and is_boilerplate(host))


def _page_genes(sink: GeneSink, d: HtmlParsed, acc: Scan, landing_host: str | None = None) -> None:
    aid = d.artifact_id
    if d.error:
        st = GeneStatus.UNAVAILABLE if d.error == FILE_TOO_LARGE else GeneStatus.FAILED
        sink.add("page_title", None, "dom_meta", artifact_id=aid, status=st, note=d.error)
        return
    if d.title:
        sink.add(
            "page_title", re.sub(r"\s+", " ", d.title).strip()[:200], "dom_meta", artifact_id=aid, dom_path="title"
        )
    if d.lang:
        sink.add("html_lang", d.lang[:10], "dom_meta", artifact_id=aid, dom_path="html[lang]", quality=0.1)
    for k in ("og:site_name", "og:title", "og:image", "og:url", "og:type", "twitter:site", "fb:app_id"):
        v = d.meta.get(k)
        if v:
            sink.add(
                "og_meta",
                f"{k}={v[:150]}",
                "dom_meta",
                artifact_id=aid,
                dom_path=f"meta[property={k}]",
                quality=0.3 if k == "og:type" else 1.0,
            )
    gen = d.meta.get("generator")
    if gen:
        sink.add("generator", gen[:80], "dom_meta", artifact_id=aid, dom_path="meta[name=generator]", quality=0.4)
    if d.canonical and d.canonical.startswith("http"):
        ch = (urlsplit(d.canonical).hostname or "").lower()
        if valid_host(ch):
            sink.add("canonical_host", ch, "dom_meta", artifact_id=aid, dom_path="link[rel=canonical]")
    # template: DOM skeleton + id/class vocabulary
    if d.tag_paths:
        sh = _shingles(d.tag_paths)
        for v in _bottom_k(sh, SHINGLE_KEEP):
            sink.add("dom_shingle", v, "dom_skeleton", artifact_id=aid, quality=0.5)
        _tlsh_gene(sink, "tlsh_html_skeleton", "\n".join(d.tag_paths).encode(), aid, {"dom_path": "body"})
        sink.add(
            "dom_skeleton_hash",
            h16("\n".join(d.tag_paths)),
            "dom_skeleton",
            artifact_id=aid,
            note=f"{len(d.tag_paths)} elements",
        )
    vocab = {f"#{i}" for i in d.ids if 3 < len(i) < 40 and not re.search(r"\d{3,}", i)}
    vocab |= {f".{c}" for c in d.classes if 3 < len(c) < 40 and not re.search(r"\d{3,}", c)}
    for v in _bottom_k({h16(x) + ":" + x for x in vocab}, 150):
        sink.add("dom_token", v.split(":", 1)[1], "dom_template", artifact_id=aid, quality=0.3)
    # scripts
    for s in d.script_src:
        sp = urlsplit(s)
        name = _clean_name(sp.path.rsplit("/", 1)[-1])
        if name and len(name) > 3:
            sink.add(
                "script_name",
                name[:80],
                "boilerplate" if _is_lib(name, sp.hostname) else None,
                artifact_id=aid,
                dom_path="script[src]",
                quality=0.2 if _is_lib(name, sp.hostname) else 0.6,
            )
        scan_text(s, "script[src]", acc, redirects=False)
    for i, code in enumerate(d.inline_scripts):
        n = norm_js(code)
        if len(n) > 60:
            sink.add("inline_script_hash", h16(n), "dom_script", artifact_id=aid, dom_path=f"script[{i}]")
        scan_text(code, f"script[{i}]", acc)
    # forms
    for i, f in enumerate(d.forms):
        fields = [x for x in f["fields"] if x and not x.startswith(("gt-", "goog"))]
        if not fields:
            continue
        sink.add("form_fields", ",".join(fields)[:300], "dom_form", artifact_id=aid, dom_path=f"form[{i}]")
        if f["hidden"]:
            sink.add(
                "lead_form_kit",
                "hidden:" + ",".join(f["hidden"])[:300],
                None,
                artifact_id=aid,
                dom_path=f"form[{i}] input[type=hidden]",
            )
        act = str(f.get("action") or "")
        if act and not act.startswith(("#", "javascript")):
            sp = urlsplit(act)
            sink.add(
                "form_action",
                (sp.hostname or "") + sp.path[:120],
                "dom_form",
                artifact_id=aid,
                dom_path=f"form[{i}][action]",
                quality=0.6,
            )
    # fake comments
    if d.comment_names:
        names = sorted({_norm_text(n) for n in d.comment_names})
        sink.add(
            "comment_names_set",
            h16("|".join(names)),
            "dom_comments",
            artifact_id=aid,
            note=f"{len(names)} commenter names",
            quality=0.8,
        )
    if d.comment_texts:
        texts = [_norm_text(t) for t in d.comment_texts]
        sink.add(
            "fake_comment_block", h16("|".join(texts)), "dom_comments", artifact_id=aid, note=f"{len(texts)} comments"
        )
        for t in sorted(set(texts))[:40]:
            if len(t) > 40:
                sink.add("fake_comment", h16(t), "dom_comments", artifact_id=aid, quality=0.7, note=t[:80])
    # brand markers
    zones = {"title": d.title or "", "og": d.meta.get("og:site_name", ""), "masthead": " ".join(d.masthead_text)}
    # G13: media/bank mark on a landing outside the brand's official domains (explanation fact, 0 bits)
    host = (urlsplit(d.saved_from).hostname or "").lower() if d.saved_from else (landing_host or "")
    emit_brand_offdomain(sink, zones, host or None, aid)
    for brand, rx in BRANDS.items():
        for zone, text in zones.items():
            if text and rx.search(text):
                sink.add("media_brand", brand, "brand", artifact_id=aid, dom_path=zone, quality=0.3)
                break
    for c in d.comments[:60]:
        sink.add("html_comment", h16(_norm_text(c)), "dom_template", artifact_id=aid, quality=0.5, note=c[:80])
    # contact tokens
    _contact_genes(sink, d.text, aid, d.raw)
    scan_text(d.raw, "html", acc)


def _contact_genes(sink: GeneSink, text: str, aid: str | None, raw: str | None = None) -> None:
    """Phones (tiered, hashed), e-mails (hashed), accounts (hashed) + bank code, wallets, messenger handles.
    `raw`: the page source, scanned for messenger links (hrefs are not in the visible text)."""
    fspans = adv_f01.foreign_spans(text) if adv_f01.enabled() else None
    for mm in PHONE_RX.finditer(text):
        m = mm.group(0)
        if fspans is not None and adv_f01.foreign_phone(text, mm.start(), mm.end(), fspans):
            continue  # ADV-F01 (CR-F01-1): national part of a +CC number
        digits = re.sub(r"\D", "", m)[-9:]
        if len(set(digits)) > 3 and not digits.startswith("0"):
            prof = phone_profile("+48" + digits)
            tier = prof.tier if prof else "operator"
            sink.add(
                "entity_phone",
                pii_hash("+48" + digits),
                f"inf:{tier}:contact",
                artifact_id=aid,
                quality=0.7,
                note=f"+48 {digits[:3]} *** **{digits[-1]}" + (f"; {prof.describe()}" if prof else ""),
            )
    for m in EMAIL_RX.findall(text):
        m = m.lower()
        if not re.search(r"\.(png|jpe?g|webp|svg|gif|js|css)$", m) and not m.endswith(("example.com", "sentry.io")):
            user, _, dom = m.partition("@")
            sink.add("entity_email", pii_hash(m), None, artifact_id=aid, note=f"{user[:1]}***@{dom}")
            sink.add("entity_email_domain", dom, None, artifact_id=aid, quality=0.6)
    for e in find_accounts(text) + find_wallets(text):
        value = pii_hash(e.value) if e.kind == "entity_iban" else e.value
        sub = {"entity_iban": "contact", "bank_code": "bank", "crypto_wallet": "crypto"}[e.kind]
        sink.add(e.kind, value, f"inf:{e.tier}:{sub}", artifact_id=aid, quality=e.quality,
                 note=f"{e.note} @{e.start}")
    for e in find_messenger_handles((raw or text)[:MAX_TEXT_SCAN]):
        value = pii_hash(e.value) if e.kind == "entity_phone" else e.value
        sink.add(e.kind, value, f"inf:{e.tier}:{e.group or 'messenger'}", artifact_id=aid, quality=e.quality,
                 note=f"{e.note} @{e.start}")


def _tlsh_gene(sink: GeneSink, kind: str, data: bytes, aid: str | None, where: dict) -> None:
    if len(data) < MIN_TLSH_BYTES:
        return
    v = tlsh_value(data)
    if v is None:
        sink.add(
            kind,
            None,
            None,
            artifact_id=aid,
            status=GeneStatus.NOT_APPLICABLE,
            note="TLSH TNULL: input too short or too uniform",
            **where,
        )
    else:
        # the tag-path skeleton is repetitive text: TLSH separates it poorly (measured), so lower prior quality
        sink.add(kind, v, None, artifact_id=aid, quality=0.3 if kind == "tlsh_html_skeleton" else 0.6, **where)


def _asset_genes(
    sink: GeneSink, name: str, data: bytes, host: str | None, aid: str | None, where: dict, acc: Scan
) -> None:
    if len(data) < MIN_ASSET:
        return
    lib = _is_lib(name, host)
    sha = hashlib.sha256(data).hexdigest()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if "favicon" in name or ext == "ico":
        sink.add("favicon_mmh3", str(favicon_hash(data)), "asset", artifact_id=aid, **where)
    if lib:
        sink.add("asset_sha256_lib", sha, "libs", artifact_id=aid, quality=0.1, note=name[:80], **where)
        return
    sink.add("asset_sha256", sha, "asset", artifact_id=aid, note=name[:80], **where)
    if ext in ("js", "mjs") or name.endswith(".js"):
        txt = data.decode("utf-8", errors="replace")
        njs = norm_js(txt)
        if len(txt) > 2000:
            sink.add("js_build_hash", h16(njs), None, artifact_id=aid, note=name[:80], **where)
        _tlsh_gene(sink, "tlsh_js", njs.encode(), aid, where)
        scan_text(txt, name[:120], acc)
    elif ext == "css":
        txt = data.decode("utf-8", errors="replace")
        if len(txt) > 1000:
            css = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
            css = re.sub(r"url\([^)]*\)", "url()", css)
            sink.add("css_build_hash", h16(re.sub(r"\s+", "", css)), None, artifact_id=aid, note=name[:80], **where)
        _tlsh_gene(sink, "tlsh_css", re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", txt, flags=re.S)).encode(), aid, where)


def _file_sets(b, pages: list[HtmlParsed]) -> list[FileSet]:
    """G06: one kit file set per saved-page folder and per HAR (first-party bodies)."""
    page_aid = {}
    for d in pages:
        src = d.src or ""
        stem = src.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem:
            page_aid[stem.lower()] = d.artifact_id
    sets: dict[str, FileSet] = {}
    for a, data in b.assets:
        path = a.meta.path_in_incident
        folder = page_folder(path)
        fs = sets.get(folder)
        if fs is None:
            stem = folder.rsplit("/", 1)[-1].lower().removesuffix("_files")
            fs = sets[folder] = FileSet(
                file_set_key(folder), folder or "_files", page_aid.get(stem, a.meta.artifact_id)
            )
        name = _clean_name(path.rsplit("/", 1)[-1])
        fs.add(path.rsplit("/", 1)[-1], data, lib=_is_lib(name, None), path=path)
    out = list(sets.values())
    for h in b.hars:
        fs = FileSet(file_set_key("har:" + h.artifact_id), "har:" + h.artifact_id[:16], h.artifact_id)
        for body in h.bodies:
            if body.url.endswith("#post") or body.mime == "text/html":
                continue
            sp = urlsplit(body.url)
            host = (sp.hostname or "").lower()
            raw = sp.path.rsplit("/", 1)[-1] or "index"
            if body.mime == "text/css" and not raw.endswith(".css"):
                raw += ".css"
            if "javascript" in body.mime and not raw.endswith(".js"):
                raw += ".js"
            fs.add(raw, body.data, lib=_is_lib(_clean_name(raw), host) or is_boilerplate(host), path=sp.path)
        if fs.n_files or fs.skipped:
            out.append(fs)
    return out


# 2: enrich-plus | operator-genes, 3: variants-pipe, 4-5: merged; 6: ADV-F01 (HYDRA_ADV_F01=1 only)
@register("inf_dom", family=Family.INF, version=adv_f01.version("inf_dom", 5))
def inf_dom(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, f"inf_dom@{adv_f01.version('inf_dom', 5)}")
    b = bundle(inc)
    acc = Scan()
    pages = list(b.htmls)
    # HTML documents captured in HARs (landing variants, iframes) count as pages too
    for h in b.hars:
        for body in h.bodies:
            host = (urlsplit(body.url).hostname or "").lower()
            if body.mime == "text/html" and len(body.data) > 500 and not is_boilerplate(host):
                pages.append(parse_html_bytes(body.data, h.artifact_id, f"har:{body.entry}"))
    if not pages and not b.assets:
        sink.missing("page_title", GeneStatus.UNAVAILABLE, "no saved landing page in the incident", "dom_meta")
        sink.missing(BAND_KIND, GeneStatus.UNAVAILABLE, "no saved landing page in the incident", BAND_GROUP)
        return sink.result()
    landing = next((h for u, _, _ in b.landing_urls if (h := (urlsplit(u).hostname or "").lower())), None)
    for d in pages:
        _page_genes(sink, d, acc, landing)
    for a, data in b.assets:
        name = _clean_name(a.meta.path_in_incident.rsplit("/", 1)[-1])
        _asset_genes(sink, name, data, None, a.meta.artifact_id, {"dom_path": a.meta.path_in_incident[-200:]}, acc)
        if name.endswith((".html", ".htm")) and len(data) > 500:
            sub = parse_html_bytes(data, a.meta.artifact_id, a.meta.path_in_incident)
            _contact_genes(sink, sub.text, a.meta.artifact_id)
    for h in b.hars:
        for body in h.bodies:
            if body.url.endswith("#post") or body.mime == "text/html":
                continue
            sp = urlsplit(body.url)
            host = (sp.hostname or "").lower()
            name = _clean_name(sp.path.rsplit("/", 1)[-1]) or "index"
            if body.mime == "text/css" and not name.endswith(".css"):
                name += ".css"
            if "javascript" in body.mime and not name.endswith(".js"):
                name += ".js"
            _asset_genes(sink, name, body.data, host, h.artifact_id, {"har_entry": body.entry}, acc)
    fsets = _file_sets(b, pages)
    for fs in fsets:
        emit_fileset(sink, fs)
    if not fsets:
        aid = pages[0].artifact_id if pages else None
        sink.add(
            BAND_KIND,
            None,
            BAND_GROUP,
            artifact_id=aid,
            status=GeneStatus.NOT_APPLICABLE,
            note="inline page: no saved first-party files",
        )
    _emit_scan(sink, acc, None, har=False)
    v = parked.incident_verdict(inc, ctx)  # ADOPT #13: a parking template describes the provider, not the operator
    if v is not None:
        n = parked.downgrade_template(sink.genes, v)
        sink.add("inf_parked", v.cls, "label", artifact_id=v.artifact_id, note=f"{v.note()}; {n} template genes 0 bits")
    return sink.result()


from hydra.genome import kit_exfil as _kit_exfil  # noqa: E402,F401  (registers inf_kit)
from hydra.genome import kit_names as _kit_names  # noqa: E402,F401  (registers inf_kit_names)
from hydra.genome import tollfraud as _tollfraud  # noqa: E402,F401  (registers inf_tollfraud)
