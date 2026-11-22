"""META family: platform metadata genes.

Sources:
- yt-dlp `.info.json` (Ad Library downloads): platform, Ad Library id, page id / name, playlist position,
  upload / download dates, duration, tool version, Ad Library URL pattern (title/description go to TXT);
- capture manifests (`manifest.json` of page-capture bundles): tool, capture time, title;
- file names: Windows "Zrzut ekranu YYYY-MM-DD HHMMSS" (local time), iOS "IMG_nnnn", iOS
  "ScreenRecording_MM-DD-YYYY HH-MM-SS", Windows "Nagrywanie ekranu YYYY-MM-DD HHMMSS";
- OCR of screenshots: advertiser name above "Sponsorowane", Ad Library panel fields (library id, start
  date, active time, impressions bucket, versions), Google "Sponsorowane · <advertiser>".
Collector times (download / capture) describe the analyst, not the campaign: quality 0.3-0.5.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import PurePath

from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.txt import _ev, _gene, dedupe, key
from hydra.genome.txt_norm import fold
from hydra.genome.txt_sources import collect, is_json

VERSION = 1
PL_MONTHS = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6, "lip": 7, "sie": 8, "wrz": 9,
             "paz": 10, "lis": 11, "gru": 12}


def _g(inc, kind, value, ev=None, q=1.0, prov=Provenance.OBS, group=None, status=GeneStatus.OBSERVED) -> Gene:
    return _gene(inc, "meta", kind, value, ev, q, group or f"meta_{kind}", prov, status=status, family=Family.META)


def _iso_date(s: str | None) -> str | None:
    if not s:
        return None
    s = str(s)
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def info_json_genes(inc, d: dict, aid: str) -> list[Gene]:
    ev = EvidencePointer(artifact_id=aid, note="info.json")
    out = []
    extractor = str(d.get("extractor") or d.get("extractor_key") or "")
    dom = str(d.get("webpage_url_domain") or "")
    platform = (extractor.split(":")[0] or dom.split(".")[0]).lower() or None
    if platform:
        out.append(_g(inc, "platform", platform, ev))
    if extractor:
        out.append(_g(inc, "source_extractor", extractor.lower(), ev, 0.5))
    ad_id = d.get("playlist_id") or (str(d.get("id") or "").split("_")[0] if "ads" in extractor else None)
    if ad_id:
        out.append(_g(inc, "platform_ad_id", str(ad_id), ev))
    for k in ("playlist_uploader_id", "uploader_id", "channel_id"):
        if d.get(k):
            out.append(_g(inc, "platform_page_id", str(d[k]), ev.model_copy(update={"note": f"info.json:{k}"})))
            break
    for k in ("playlist_uploader", "uploader", "channel"):
        if d.get(k):
            out.append(_g(inc, "platform_page_name", key(str(d[k])),
                          ev.model_copy(update={"note": f"info.json:{k} = {d[k]}"})))
            break
    url = d.get("webpage_url") or d.get("playlist_webpage_url")
    if url:
        pattern = re.sub(r"\d{6,}", "<id>", re.sub(r"^https?://(www\.)?", "", str(url)))
        out.append(_g(inc, "ad_library_url_pattern", pattern, ev, 0.5))
    if d.get("n_entries") or d.get("playlist_count"):
        out.append(_g(inc, "ad_versions", str(d.get("n_entries") or d.get("playlist_count")), ev, 0.6))
    for k, kind in (("upload_date", "upload_date"), ("release_date", "release_date"), ("modified_date", "upload_date")):
        v = _iso_date(d.get(k))
        if v:
            out.append(_g(inc, kind, v, ev.model_copy(update={"note": f"info.json:{k}"})))
    if d.get("timestamp"):
        try:
            out.append(_g(inc, "upload_time", datetime.fromtimestamp(int(d["timestamp"]), UTC).isoformat(), ev))
        except (ValueError, OverflowError, OSError):
            pass
    if d.get("epoch"):
        try:
            t = datetime.fromtimestamp(int(d["epoch"]), UTC)
            out.append(_g(inc, "collected_date", t.date().isoformat(),
                          ev.model_copy(update={"note": f"yt-dlp download time {t.isoformat()} (collector)"}), 0.3))
        except (ValueError, OverflowError, OSError):
            pass
    if d.get("duration"):
        try:
            out.append(_g(inc, "media_duration_s", str(round(float(d["duration"]))), ev, 0.5))
        except (TypeError, ValueError):
            pass
    ver = d.get("_version")
    if isinstance(ver, dict) and ver.get("version"):
        out.append(_g(inc, "capture_tool", f"yt-dlp {ver['version']}", ev, 0.2))
    if not d.get("description"):
        out.append(_g(inc, "ad_text", None, ev.model_copy(update={"note": "info.json has no description/ad text"}),
                      status=GeneStatus.UNAVAILABLE))
    return out


def manifest_genes(inc, d: dict, aid: str) -> list[Gene]:
    ev = EvidencePointer(artifact_id=aid, note="capture manifest")
    out = []
    if d.get("tool"):
        out.append(_g(inc, "capture_tool", str(d["tool"]), ev, 0.2))
    if d.get("captured_at"):
        out.append(_g(inc, "collected_date", str(d["captured_at"])[:10],
                      ev.model_copy(update={"note": f"captured_at {d['captured_at']} (collector)"}), 0.3))
    return out


_FN_PATTERNS = (
    (re.compile(r"(?:Zrzut ekranu|Screenshot)[ _](\d{4})-(\d{2})-(\d{2})[ _](\d{2})(\d{2})(\d{2})"), "ymd", "windows",
     "screenshot"),
    (re.compile(r"Nagrywanie ekranu (\d{4})-(\d{2})-(\d{2}) (\d{2})(\d{2})(\d{2})"), "ymd", "windows", "recording"),
    (re.compile(r"ScreenRecording_(\d{2})-(\d{2})-(\d{4})[ _](\d{2})-(\d{2})-(\d{2})"), "mdy", "ios", "recording"),
    (re.compile(r"Screen Recording (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})"), "ymd", "macos",
     "recording"),
    (re.compile(r"Screenshot_(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})"), "ymd", "android", "screenshot"),
)


def filename_genes(inc, name: str, aid: str) -> list[Gene]:
    out = []
    base = PurePath(name).name
    for rx, order, device, what in _FN_PATTERNS:
        m = rx.search(base)
        if not m:
            continue
        g = m.groups()
        y, mo, d = (g[0], g[1], g[2]) if order == "ymd" else (g[2], g[0], g[1])
        ts = f"{y}-{mo}-{d}T{g[3].zfill(2)}:{g[4]}:{g[5]}"
        ev = EvidencePointer(artifact_id=aid, note=f"file name {base!r}: {what}, local time of the collector device")
        out += [_g(inc, "capture_date", f"{y}-{mo}-{d}", ev, 0.5), _g(inc, "capture_time", ts, ev, 0.3),
                _g(inc, "capture_device", device, ev, 0.2)]
        return out
    if re.fullmatch(r"IMG_\d{4}\.(PNG|JPG|JPEG|HEIC)", base, re.I):
        out.append(_g(inc, "capture_device", "ios", EvidencePointer(artifact_id=aid, note=f"file name {base!r}"), 0.2))
    return out


_LIB_ID = re.compile(r"(?:identyfikator biblioteki|library id)\s*:?\s*(\d{10,20})")
_LIB_DATE = re.compile(r"(?:data zamieszczenia|rozpoczecie wyswietlania|started running on)\s*:?\s*(\d{1,2})\s+"
                       r"([a-z]{3})[a-z]*\s+(\d{4})")
_LIB_ACTIVE = re.compile(r"(?:czas aktywnosci|aktywnosci)\s*:?\s*(\d+)\s*(godz|dni|dzien|min)")
_LIB_IMPR = re.compile(r"wyswietlenia\s*:?\s*(<?\s*\d[\d\s]*(?:tys)?)")
_LIB_VERS = re.compile(r"(\d+)\s+wersj")


def ocr_genes(inc, texts) -> list[Gene]:
    out = []
    for b in texts.blocks:
        if not b.source.startswith("ocr"):
            continue
        segs = b.segments
        for i, s in enumerate(segs):
            f = " ".join(fold(s.text).split())
            if f in ("sponsorowane", "sponsored") and i > 0:
                prev = segs[i - 1]
                if not prev.chrome and 2 <= len(prev.text) <= 60 and prev.zone in ("ad", "library_ad"):
                    out.append(_g(inc, "platform_page_name", key(prev.text), _ev(prev, note=f"advertiser above "
                                  f"'Sponsorowane': {prev.text}"), 0.7 * prev.quality, Provenance.OBS))
            m = re.match(r"^(?:sponsorowane|sponsored)\s+(.{2,60})$", f)
            if m and "·" in s.text or (m and "-" in s.raw):
                name = re.split(r"[·•-]", s.text, maxsplit=1)[-1].strip()
                out.append(_g(inc, "platform_page_name", key(name), _ev(s, note=s.text), 0.6))
                out.append(_g(inc, "platform", "google", _ev(s, note="'Sponsorowane · <advertiser>' (Google ads "
                              "format)"), 0.5, Provenance.INF))
            for rx, kind in ((_LIB_ID, "platform_ad_id"),):
                mm = rx.search(f)
                if mm:
                    out.append(_g(inc, kind, mm.group(1), _ev(s, note=s.text), 0.9 * s.quality))
                    out.append(_g(inc, "platform", "meta", _ev(s, note="Meta Ad Library panel"), 0.6,
                                  Provenance.INF))
            mm = _LIB_DATE.search(f)
            if mm and mm.group(2)[:3] in PL_MONTHS:
                d = f"{mm.group(3)}-{PL_MONTHS[mm.group(2)[:3]]:02d}-{int(mm.group(1)):02d}"
                out.append(_g(inc, "ad_start_date", d, _ev(s, note=s.text), 0.8 * s.quality))
            mm = _LIB_ACTIVE.search(f)
            if mm:
                out.append(_g(inc, "ad_active_time", f"{mm.group(1)} {mm.group(2)}", _ev(s, note=s.text), 0.4))
            mm = _LIB_IMPR.search(f)
            if mm and s.zone.startswith("library"):
                out.append(_g(inc, "ad_impressions", mm.group(1).replace(" ", ""), _ev(s, note=s.text), 0.3))
            mm = _LIB_VERS.search(f)
            if mm and s.zone.startswith("library"):
                out.append(_g(inc, "ad_versions", mm.group(1), _ev(s, note=s.text), 0.5))
            if f in ("instagram",) and s.zone != "ad":
                out.append(_g(inc, "placement", "instagram", _ev(s, note=s.text), 0.4, Provenance.INF))
    return out


@register("meta", family=Family.META, version=VERSION)
def meta(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    genes: list[Gene] = []
    for a in inc.artifacts:
        aid = a.meta.artifact_id
        genes.extend(filename_genes(inc, a.meta.path_in_incident, aid))
        if not is_json(a) or a.meta.role in (ArtifactRole.LANDING_ASSET, ArtifactRole.HAR):
            continue
        if a.meta.size > 20_000_000:
            continue
        try:
            d = json.loads(a.path.read_text("utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError) as exc:
            if a.meta.role == ArtifactRole.PLATFORM_METADATA:
                genes.append(_g(inc, "platform_metadata", None, EvidencePointer(artifact_id=aid, note=str(exc)[:200]),
                                status=GeneStatus.FAILED))
            continue
        if not isinstance(d, dict):
            continue
        if "extractor" in d or "extractor_key" in d or "webpage_url" in d:
            genes.extend(info_json_genes(inc, d, aid))
        elif "tool" in d and ("captured_at" in d or "page_url" in d):
            genes.extend(manifest_genes(inc, d, aid))
    try:
        genes.extend(ocr_genes(inc, collect(inc, ctx)))
    except Exception as exc:  # noqa: BLE001
        genes.append(_g(inc, "meta_ocr", None, EvidencePointer(note=f"{type(exc).__name__}: {exc}"[:200]),
                        status=GeneStatus.FAILED))
    return ExtractResult(genes=dedupe(genes))
