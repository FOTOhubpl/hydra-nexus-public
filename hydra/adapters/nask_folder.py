"""NASK sample adapter: one folder per incident → `Incident` + artifacts with roles.

Observed sample layout (docs/core/CORE_LOG.md): ad screenshots (`Zrzut ekranu …png`, `IMG_*.PNG`,
`reklama_1.png`), screen recordings (`ScreenRecording_*.MP4`, `Nagrywanie ekranu *.mp4`), a Facebook ad video
with a yt-dlp `.info.json`, saved landing pages (`*.html` + `*_files/`, or a capture folder with `page.mhtml`,
`dom.html`, `page.har`, `manifest.json`, `resources/`, `screenshot.png`), HAR network logs (one is named just
`.har`), and sub-pages `strona_1..3`. Saved-page resources often carry browser download suffixes
(`main.js.pobrane`, `*.pobierz`) or no extension at all, so roles and MIME types come from sniffing.

Pages are hostile: this module only reads bytes, never renders or executes anything. HAR files are only
read for their main document host (mis-filed-HAR note, local-replay flag); the genome layer parses them.
MHTML archives are split into their parts, each stored as a derived artifact (meta `derived_from`), so the
HTML and its resources are analysed like a saved folder.
"""

from __future__ import annotations

import contextlib
import contextvars
import email
import email.policy
import hashlib
import json
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hydra import events, import_progress
from hydra.config import Settings
from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.db import Writer
from hydra.ingest import register_artifact, sniff_mime, zip_entry_encrypted, zip_entry_name
from hydra.jobs import new_job_id
from hydra.redact import redact_text, redact_url
from hydra.storage import ContentStore, sha256_file

DOC_LIMIT_MB = 200  # HAR / HTML / MHTML / images; videos may be larger
DOWNLOAD_SUFFIXES = (".pobrane", ".pobierz", ".download", ".crdownload")
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".localized", ".directory", "icon\r"}
SKIP_DIRS = {"__MACOSX", ".git", ".svn", ".hg", ".idea", ".vscode", ".venv", "__pycache__", ".Trashes",
             ".Spotlight-V100", ".fseventsd"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".avif", ".svg", ".ico"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".ts"}
TEXT_EXT = {".txt", ".log", ".md", ".csv", ".eml"}
EXT_MIME = {
    ".js": "application/javascript", ".mjs": "application/javascript", ".css": "text/css",
    ".svg": "image/svg+xml", ".json": "application/json", ".m3u8": "application/vnd.apple.mpegurl",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".ico": "image/x-icon",
    ".html": "text/html", ".htm": "text/html", ".har": "application/json", ".ts": "video/mp2t",
}
CREATIVE_HINTS = ("reklama", "creative", "kreacja", "ad_", "ad-", "banner")
DATE_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
US_DATE_RE = re.compile(r"ScreenRecording_(\d{2})-(\d{2})-(20\d{2})", re.I)  # iOS: MM-DD-YYYY (FORMATS.md §32)
CAPTURE_SHA_RE = re.compile(r"^(?P<stem>.+)_[0-9a-f]{8}(?P<ext>\.[A-Za-z0-9]{1,6})$")  # resources/x_<sha8>.ext
STAGE_RE = re.compile(r"^strona[_ -]?(\d+)$", re.I)
URL_ONLY_RE = re.compile(r"^\s*(https?://\S+|www\.\S+)\s*$", re.I)


@dataclass
class PlannedArtifact:
    rel: str
    role: ArtifactRole
    mime: str
    size: int
    path: Path | None = None
    data: bytes | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    digest: str | None = None


@dataclass
class ImportReport:
    incident: Incident
    counts: dict[str, int]
    new_artifacts: int
    files_seen: int
    derived: int
    errors: list[dict[str, str]] = field(default_factory=list)


def logical_name(name: str) -> str:
    """`main.js.pobrane` → `main.js`; `fbevents.pobrane` → `fbevents`; capture `jquery.min_0a1b2c3d.js` →
    `jquery.min.js`."""
    low = name.lower()
    for suf in DOWNLOAD_SUFFIXES:
        if low.endswith(suf):
            return name[: -len(suf)]
    m = CAPTURE_SHA_RE.match(name)
    if m:
        return m.group("stem") + m.group("ext")
    return name


def _ext(name: str) -> str:
    n = logical_name(name).lower()
    if n.endswith(".info.json"):
        return ".info.json"
    if n == ".har" or n.endswith(".har"):
        return ".har"
    dot = n.rfind(".")
    if dot == 0:
        return n  # a dot-file named only by its extension: `.har`, `.json`, `.html`
    if dot < 0:
        return ""
    ext = n[dot:]
    return ext.split("@", 1)[0]  # `index.js@latest`


def _head(path: Path, n: int = 4096) -> bytes:
    with path.open("rb") as fh:
        return fh.read(n)


def detect_mime(path: Path) -> str:
    """Magic bytes first; then the logical extension; then text sniffing."""
    sniffed = sniff_mime(path)
    ext = _ext(path.name)
    head = _head(path, 2048).lstrip()
    if head[:5].lower() == b"from:" or b"multipart/related" in _head(path, 4096).lower():
        if b"mime-version" in _head(path, 4096).lower():
            return "multipart/related"
    if sniffed.startswith(("image/", "video/", "audio/")) or sniffed in ("application/pdf", "application/zip"):
        return sniffed
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in _head(path, 4096)):
        return "image/svg+xml"
    if ext in EXT_MIME and sniffed in ("text/plain", "application/octet-stream", "application/json", "text/html"):
        if ext == ".har" or not (sniffed == "text/html" and ext not in (".html", ".htm")):
            return EXT_MIME[ext]
    if sniffed == "text/plain" and ext not in TEXT_EXT:
        # extensionless / truncated saved resources (`js`, `css(1)`, `m=el_main_css`, `fbevents.pobrane`,
        # pixel-id configs, `sweetalert2@11`): JS vs CSS from the text itself (FORMATS.md §8, §10)
        return _sniff_code(head.decode("utf-8", errors="replace")) or sniffed
    return sniffed


CSS_START_RE = re.compile(r"^\s*(@charset|@import|@font-face|@media|@keyframes|:root\b)", re.I)
CSS_RULE_RE = re.compile(r"^\s*(/\*.*?\*/\s*)*[\w\-.#:\[\]=\"'*,>~+()\s]+\{\s*[\w-]+\s*:[^;{}]+[;}]", re.S)
JS_RE = re.compile(r"\b(function|var|let|const|return|typeof|window|document|undefined)\b|=>|!function|"
                   r"\(function|use strict|__webpack|\bfbq\b|\bgtag\(")


def _sniff_code(text: str) -> str | None:
    if CSS_START_RE.match(text):
        return "text/css"
    if JS_RE.search(text[:4096]):
        return "application/javascript"
    if CSS_RULE_RE.match(text[:4096]):
        return "text/css"
    return None



def _is_asset_dir(part: str) -> bool:
    low = part.lower()
    return low.endswith("_files") or low in ("resources", "assets")


def _page_key(rel_parts: tuple[str, ...]) -> str:
    """The landing page a file belongs to: the folder holding the page (assets map to their page)."""
    parts = list(rel_parts[:-1])
    for i, p in enumerate(parts):
        if _is_asset_dir(p):
            parts = parts[:i]
            break
    return "/".join(parts)


def classify(rel_parts: tuple[str, ...], mime: str, path: Path, siblings: set[str]) -> tuple[ArtifactRole, dict]:
    name = rel_parts[-1]
    low = logical_name(name).lower()
    ext = _ext(name)
    meta: dict[str, Any] = {}
    in_assets = any(_is_asset_dir(p) for p in rel_parts[:-1])
    if ext == ".har":
        return ArtifactRole.HAR, meta
    if mime == "application/json" and not in_assets:
        head = _head(path, 4096)
        if b'"log"' in head and (b'"entries"' in head or b'"creator"' in head):
            return ArtifactRole.HAR, meta
    if ext == ".info.json":
        return ArtifactRole.PLATFORM_METADATA, {"kind": "ytdlp_info"}
    if low == "manifest.json" and not in_assets:
        return ArtifactRole.PLATFORM_METADATA, {"kind": "capture_manifest"}
    if in_assets:
        return ArtifactRole.LANDING_ASSET, meta
    if mime == "application/json":
        from hydra.genome.chat import looks_like_chat_json

        if looks_like_chat_json(_head(path, 8192)):  # Telegram / Messenger / generic chat export
            return ArtifactRole.TEXT, {"format": "chat_json"}
    if mime == "application/pdf" or ext == ".pdf":
        return ArtifactRole.TEXT, {"format": "pdf"}
    if mime == "multipart/related" or ext in (".mhtml", ".mht"):
        return ArtifactRole.LANDING_HTML, {"format": "mhtml"}
    if mime == "text/html" or ext in (".html", ".htm"):
        return ArtifactRole.LANDING_HTML, {"format": "rendered_dom" if low == "dom.html" else "html"}
    if mime.startswith("video/") or ext in VIDEO_EXT:
        stem = logical_name(name).rsplit(".", 1)[0]
        if f"{stem}.info.json".lower() in siblings:
            return ArtifactRole.AD_VIDEO, meta
        return ArtifactRole.SCREEN_RECORDING, meta
    if mime.startswith("image/") or ext in IMAGE_EXT:
        if low.startswith("screenshot") and ("manifest.json" in siblings or "page.har" in siblings):
            return ArtifactRole.LANDING_SCREENSHOT, meta
        if any(h in low for h in CREATIVE_HINTS):
            return ArtifactRole.AD_CREATIVE, meta
        return ArtifactRole.FEED_SCREENSHOT, meta
    if mime.startswith("text/") or ext in TEXT_EXT:
        try:
            text = _head(path, 4096).decode("utf-8", errors="replace")  # head only: never the whole file
        except OSError:
            text = ""
        if URL_ONLY_RE.match(text):
            return ArtifactRole.URL, {"url": redact_url(text.strip())}
        return ArtifactRole.TEXT, meta
    return ArtifactRole.OTHER, meta


DOT_DATA_EXT = {".har", ".json", ".info.json", ".html", ".htm", ".mhtml", ".mht", ".m3u8"}


def is_junk(rel_parts: tuple[str, ...]) -> bool:
    """OS / VCS / archiver junk (`.DS_Store`, `._x`, `__MACOSX/`, `.git/`, `Thumbs.db`): silently ignored."""
    name = rel_parts[-1]
    if name.lower() in SKIP_NAMES or name.startswith("._"):
        return True
    return any(p in SKIP_DIRS for p in rel_parts[:-1])


def is_data_file(name: str, head: bytes = b"") -> bool:
    """The shared data-vs-junk rule for importers, the API and the UI (audit A03).

    `name` may be a bare file name or a relative path (`/` or `\\`); `head` is the first bytes of the file (512+
    is enough; empty when unknown). OS / VCS / archiver junk (`.DS_Store`, `._x`, `__MACOSX/`, `.git/`,
    `Thumbs.db`) → False. A visible file → True. A hidden file (or one inside a hidden folder) → True only when it
    is data: by logical extension (`.har`, `.json`, `.html`, `.mhtml`, `.m3u8`, `.info.json`) or by its bytes
    (image / video / audio / PDF magic, JSON, HTML, MHTML). `61/strona_1/.har` is data; `.gitignore` is not."""
    parts = tuple(p for p in name.replace("\\", "/").split("/") if p not in ("", "."))
    if not parts or is_junk(parts):
        return False
    if not any(p.startswith(".") for p in parts):
        return True
    if _ext(parts[-1]) in DOT_DATA_EXT:
        return True
    if not head:
        return False
    import filetype

    kind = filetype.guess(head)
    if kind is not None and (kind.mime.startswith(("image/", "video/", "audio/")) or kind.mime == "application/pdf"):
        return True
    text = head.lstrip().lower()
    return text.startswith((b"{", b"<!doctype html", b"<html", b"mime-version", b"from: <saved by", b"<svg"))


def is_data_dotfile(path: Path) -> bool:
    """`is_data_file` for a file on disk."""
    try:
        head = _head(path, 4096)
    except OSError:
        return False
    return is_data_file(path.name, head)


def _walk(folder: Path, skipped: list[dict[str, str]] | None = None) -> list[Path]:
    out = []
    for f in sorted(folder.rglob("*")):
        if f.is_symlink():
            # never followed (a link may point outside the dataset), but reported: nothing disappears silently
            if skipped is not None and not is_junk(f.relative_to(folder).parts):
                skipped.append({"path": str(f.relative_to(folder)), "code": "SYMLINK_NOT_FOLLOWED",
                                "reason": "symbolic link: not followed"})
            continue
        if not f.is_file():
            continue
        rel = f.relative_to(folder)
        # Dot-files with data are kept (a HAR named just `.har`, FORMATS.md §22); OS/VCS junk is skipped.
        if is_junk(rel.parts):
            continue
        if any(p.startswith(".") for p in rel.parts) and not is_data_dotfile(f):
            if skipped is not None:
                skipped.append({"path": str(rel), "code": "HIDDEN_NOT_DATA",
                                "reason": "hidden file that is not a recognised data format"})
            continue
        out.append(f)
    return out


def split_mhtml(data: bytes) -> list[tuple[str, str, bytes]]:
    """MHTML → [(content_type, content_location, payload)]. Pure parsing, nothing is executed."""
    msg = email.message_from_bytes(data, policy=email.policy.compat32)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        parts.append((part.get_content_type(), str(part.get("Content-Location") or ""), payload))
    return parts


def _dates_from_name(name: str) -> list[str]:
    out = []
    for m, d, y in US_DATE_RE.findall(name):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            out.append(f"{y}-{m}-{d}")
    for y, m, d in DATE_RE.findall(name):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            out.append(f"{y}-{m}-{d}")
    return out


NESTED_ZIP_DEPTH = 2  # ZIPs inside an incident folder are unpacked (same guards) up to this depth


def plan_incident(
    folder: Path, max_file_bytes: int, skipped: list[dict[str, str]] | None = None,
    settings: Settings | None = None, depth: int = 0,
) -> tuple[list[PlannedArtifact], list[dict[str, str]], list[str]]:
    files = _walk(folder, skipped)
    by_dir: dict[Path, set[str]] = {}
    for f in files:
        by_dir.setdefault(f.parent, set()).add(logical_name(f.name).lower())
    planned: list[PlannedArtifact] = []
    errors: list[dict[str, str]] = []
    dates: list[str] = []
    for f in files:
        rel = f.relative_to(folder)
        size = f.stat().st_size
        if size == 0:
            errors.append({"path": str(rel), "code": "FILE_EMPTY"})
            continue
        if size > max_file_bytes:
            errors.append({"path": str(rel), "code": "FILE_TOO_LARGE"})
            continue
        try:
            mime = detect_mime(f)
            role, meta = classify(rel.parts, mime, f, by_dir.get(f.parent, set()))
        except OSError as exc:
            errors.append({"path": str(rel), "code": type(exc).__name__})
            continue
        meta["page"] = _page_key(rel.parts)
        meta["name"] = logical_name(f.name)
        stage = _stage(meta["page"])
        if stage is not None:
            meta["stage"] = stage
        dates.extend(_dates_from_name(f.name))
        if (mime.startswith("image/hei") or f.suffix.lower() in (".heic", ".heif")) and size <= DOC_LIMIT_MB * MB:
            png = heic_to_png(f)  # PIL cannot read HEIC: a PNG rendition carries the image genes (audit B14)
            if png is not None:
                planned.append(PlannedArtifact(f"{rel}.png", role, "image/png", len(png), data=png,
                                               meta={**meta, "derived_from": str(rel), "format": "heic_converted"}))
                role, meta = ArtifactRole.OTHER, {**meta, "converted_to": f"{rel}.png"}
            else:
                errors.append({"path": str(rel), "code": "HEIC_UNREADABLE"})
        planned.append(PlannedArtifact(str(rel), role, mime, size, path=f, meta=meta))
        if depth < NESTED_ZIP_DEPTH and _is_archive(f):
            # an archive inside the incident (an incident ZIP holding another ZIP): its files belong to this incident
            try:
                inner = unpack_zip(f, settings)
            except ZipRejected as exc:
                errors.append({"path": str(rel), "code": exc.code})
                inner = None
            except Exception as exc:  # noqa: BLE001 — unreadable inner archive: reported, the rest imports
                errors.append({"path": str(rel), "code": f"ZIP_UNREADABLE:{type(exc).__name__}"})
                inner = None
            if inner is not None:
                # entries the guards dropped from the inner archive (traversal, symlink, encrypted): reported too
                errors.extend({"path": f"{rel}!/{e.get('path', '')}", "code": str(e.get("code", ""))}
                              for e in zip_skipped(f, settings))
                sub, sub_err, sub_dates = plan_incident(inner, max_file_bytes, skipped, settings, depth + 1)
                for pa in sub:
                    pa.rel = f"{rel}!/{pa.rel}"
                    pa.meta["page"] = f"{rel}!/{pa.meta.get('page', '')}".rstrip("/")
                planned.extend(sub)
                errors.extend({**e, "path": f"{rel}!/{e.get('path', '')}"} for e in sub_err)
                dates.extend(sub_dates)
        if role == ArtifactRole.PLATFORM_METADATA:
            dates.extend(_dates_from_metadata(f))
        if role == ArtifactRole.LANDING_HTML and meta.get("format") == "mhtml" and size <= DOC_LIMIT_MB * 1024 * 1024:
            planned.extend(_mhtml_parts(f, str(rel), meta["page"], errors, dates))
    _flag_misfiled_hars(planned)
    return planned, errors, dates


def heic_to_png(path: Path) -> bytes | None:
    """First image of a HEIC/HEIF file as PNG bytes: ffmpeg (local files only; HEIF items need ffmpeg >= 7.1), else
    macOS `sips`. None when neither can read it (the file is then reported `HEIC_UNREADABLE`)."""
    import shutil
    import subprocess
    import tempfile

    exe = shutil.which("ffmpeg")
    if exe:
        try:
            r = subprocess.run([exe, "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", str(path),
                                "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
                               capture_output=True, timeout=60, check=False)
            if r.returncode == 0 and r.stdout.startswith(b"\x89PNG"):
                return r.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    sips = shutil.which("sips")
    if sips:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.png"
            try:
                subprocess.run([sips, "-s", "format", "png", str(path), "--out", str(out)], capture_output=True,
                               timeout=60, check=False)
            except (OSError, subprocess.SubprocessError):
                return None
            if out.is_file() and out.read_bytes().startswith(b"\x89PNG"):
                return out.read_bytes()
    return None


# ------------------------------------------------------------------------------- HAR vs landing page

MB = 1024 * 1024
HAR_PARSE_MAX = 64 * MB
HAR_URL_RE = re.compile(rb'"url"\s*:\s*"((?:https?|file)://[^"\\]+)"')
MISFILED_NOTE = "HAR entry host not referenced by this incident's landing page"


def har_main_document(path: Path) -> str | None:
    """URL of the HAR's main document (first top-level document entry; fallback: page title URL, first entry).
    Pure JSON reading; larger files fall back to a regex over the first 4 MB."""
    size = path.stat().st_size
    if size > HAR_PARSE_MAX:
        m = HAR_URL_RE.search(_head(path, 4 * MB))
        return m.group(1).decode("utf-8", "replace") if m else None
    try:
        log = json.loads(path.read_bytes()).get("log") or {}
    except (ValueError, OSError, AttributeError):
        return None

    def url(e: dict) -> str:
        return str((e.get("request") or {}).get("url") or "")

    entries = [e for e in log.get("entries") or [] if isinstance(e, dict)]
    titles = [str((pg or {}).get("title") or "") for pg in log.get("pages") or [] if isinstance(pg, dict)]
    if any(t.startswith("file:") for t in titles) or any(url(e).startswith("file:") for e in entries[:5]):
        return next(t for t in [*titles, *map(url, entries)] if t.startswith("file:"))  # local replay
    for e in entries:
        mt = str(((e.get("response") or {}).get("content") or {}).get("mimeType") or "")
        if (e.get("_resourceType") == "document" or mt.startswith("text/html")) and url(e).startswith("http"):
            return url(e)
    for t in titles:
        if t.startswith(("http://", "https://")):
            return t
    return url(entries[0]) if entries else None


def _host(url: str) -> str | None:
    from urllib.parse import urlsplit

    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    return h.removeprefix("www.") or None


def _flag_misfiled_hars(planned: list[PlannedArtifact]) -> None:
    """Mis-filed material (e.g. one incident folder holding another incident's landing HAR) is kept where it was
    filed, but a HAR whose main document host is absent from every saved page of the incident gets an analyst
    note. No saved page in the incident → unknown → no note (missing data is never evidence)."""
    pages: list[bytes] = []
    for pa in planned:
        if pa.role != ArtifactRole.LANDING_HTML or pa.meta.get("format") == "mhtml":
            continue
        if pa.size > DOC_LIMIT_MB * MB:  # quarantined later anyway; never read
            continue
        try:
            pages.append((pa.data if pa.data is not None else pa.path.read_bytes() if pa.path else b"").lower())
        except OSError:
            continue
    for pa in planned:
        if pa.role != ArtifactRole.HAR or pa.path is None:
            continue
        try:
            main = har_main_document(pa.path)
        except OSError:
            continue
        if not main:
            continue
        if main.startswith("file:"):
            pa.meta["har_kind"] = "replay"  # local replay of a saved page (FORMATS.md §23); path not stored
            continue
        host = _host(main)
        if not host:
            continue
        pa.meta["main_host"] = host
        if pages and not any(host.encode() in pg for pg in pages):
            pa.meta["note"] = MISFILED_NOTE


def _stage(page: str) -> int | None:
    """Ordered funnel pages (`strona_1..3`: black page / lead form / white page) → 1..n."""
    first = page.split("/", 1)[0] if page else ""
    m = STAGE_RE.match(first)
    return int(m.group(1)) if m else None


def _dates_from_metadata(path: Path) -> list[str]:
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError):
        return []
    out = []
    from datetime import UTC, datetime

    for key in ("timestamp", "epoch", "release_timestamp"):
        v = d.get(key) if isinstance(d, dict) else None
        if isinstance(v, int | float) and v > 1e9:
            out.append(datetime.fromtimestamp(v, UTC).date().isoformat())
            break
    for key in ("captured_at", "upload_date"):
        v = d.get(key) if isinstance(d, dict) else None
        if isinstance(v, str) and v:
            out.append(f"{v[:4]}-{v[4:6]}-{v[6:8]}" if v.isdigit() and len(v) == 8 else v[:10])
    return out


def _mhtml_parts(path: Path, rel: str, page: str, errors: list, dates: list) -> list[PlannedArtifact]:
    data = path.read_bytes()
    out = []
    try:
        msg_head = data[:4096].decode("utf-8", errors="replace")
        m = re.search(r"^Date:\s*(.+)$", msg_head, re.M)
        if m:
            from email.utils import parsedate_to_datetime

            try:
                dates.append(parsedate_to_datetime(m.group(1).strip()).date().isoformat())
            except (TypeError, ValueError):
                pass
        parts = split_mhtml(data)
    except Exception as exc:  # noqa: BLE001 — a broken archive is reported, the incident still imports
        errors.append({"path": rel, "code": f"MHTML_UNREADABLE:{type(exc).__name__}"})
        return out
    main_done = False
    for i, (ctype, loc, payload) in enumerate(parts):
        if not payload:
            continue
        if ctype == "text/html" and not main_done:
            role, fmt = ArtifactRole.LANDING_HTML, "html_from_mhtml"
            main_done = True
        else:
            role, fmt = ArtifactRole.LANDING_ASSET, None
        meta = {"page": page, "derived_from_path": rel, "url": redact_url(loc), "part": i}
        stage = _stage(page)
        if stage is not None:
            meta["stage"] = stage
        if fmt:
            meta["format"] = fmt
        name = loc.rsplit("/", 1)[-1].split("?", 1)[0][:80] if loc else f"part{i}"
        meta["name"] = name or f"part{i}"
        out.append(PlannedArtifact(f"{rel}#part{i:03d}", role, ctype, len(payload), data=payload, meta=meta))
    return out


def _store(store: ContentStore, pa: PlannedArtifact, limit: int) -> None:
    if pa.data is not None:
        pa.digest = store.put_bytes(pa.data)
        pa.data = None  # free memory; bytes now live in the CAS
    else:
        assert pa.path is not None
        pa.digest, pa.size = store.put_file(pa.path, max_bytes=limit)


def _slug(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")
    return out[:60] or hashlib.sha256(name.encode()).hexdigest()[:8]


def incident_id_for(folder: Path, prefix: str = "nask") -> str:
    """Readable id `<prefix>-<folder name>`. Uniqueness across datasets is enforced by `_unique_id`."""
    return f"{prefix}-{_slug(folder.name)}"


_ID_DIGEST_STEPS = (6, 12, 20, 32, 64)  # 6 = the historical suffix length; extended only on a real collision


def source_ref_for(folder: Path, settings: Settings) -> str:
    """Stored `source_ref` of an incident folder (CR-E2E-6). A folder inside the server scratch dir (unpacked ZIP,
    upload spool, URL/single-file temp dir) is deleted after import, so its absolute path is meaningless and
    discloses the state location: it is stored relative to the scratch dir, without the `unzip-<sha16>` level
    (`zip-c0c7f26e/base/a1`, `analyse-k2x9/a1`). The first kept component is per-archive / per-spool, so two
    sources never share a ref. Any other folder (a local organiser folder) keeps its absolute path."""
    if settings.state_dir is not None:
        try:
            rel = folder.relative_to(settings.tmp_dir.resolve())
        except ValueError:
            rel = None
        if rel is not None and rel.parts:
            parts = rel.parts[1:] if rel.parts[0].startswith("unzip-") and len(rel.parts) > 1 else rel.parts
            return "/".join(parts)
    return str(folder)


def _unique_id(conn: sqlite3.Connection, iid: str, folder: Path, src: str | None = None) -> str:
    """Id for the source root `folder`, resolved inside the writer transaction that writes the incident
    (CR-ASTRA-PIPE-102): never merge two different source folders under one id.

    1. A root imported before keeps its id (`iid` or a `iid-<digest>` it already owns): reimport is idempotent.
    2. Else `iid` when free (readable ids for the first dataset).
    3. Else `iid-<sha256(path)[:n]>`, n = 6, 12, ... 64, the first candidate that is free or owned by this root.
       The full 64-hex digest is the source identity; shorter prefixes are only a readability shortcut and are
       checked for ownership, so a short-digest collision extends the suffix instead of merging.
    `src` is the stored ref (`source_ref_for`); rows written before CR-E2E-6 hold the absolute `folder` path and
    still count as owned by this source."""
    legacy = str(folder)
    src = src or legacy
    owners = {src, legacy}
    owned = conn.execute("SELECT id FROM incidents WHERE source_ref IN (?, ?) AND (id = ? OR substr(id, 1, ?) = ?)"
                         " ORDER BY length(id), id LIMIT 1", (src, legacy, iid, len(iid) + 1, iid + "-")).fetchone()
    if owned is not None:
        return owned["id"]

    def _owner(cand: str) -> str | None:
        row = conn.execute("SELECT source_ref FROM incidents WHERE id = ?", (cand,)).fetchone()
        return row["source_ref"] if row else None

    if _owner(iid) is None:
        return iid
    digest = hashlib.sha256(src.encode()).hexdigest()
    for n in _ID_DIGEST_STEPS:
        cand = f"{iid}-{digest[:n]}"
        if _owner(cand) in (None, *owners):
            return cand
    raise RuntimeError(f"incident id {iid!r}: full source digest already owned by another source")


def import_incident(
    folder: Path,
    writer: Writer,
    store: ContentStore,
    settings: Settings,
    *,
    incident_id: str | None = None,
    source: str = "nask-sample",
    label: str | None = None,
    roles: dict[str, ArtifactRole] | None = None,
    prefix: str = "nask",
) -> ImportReport:
    """Import one incident folder. Idempotent: same bytes → same artifact ids; links are INSERT OR IGNORE.
    Importing another folder under the same `incident_id` adds its artifacts to the incident (merge).
    `roles` overrides the sniffed role per relative path (e.g. a screenshot given as the ad creative)."""
    folder = folder.expanduser().resolve()
    # Local organiser folders: screen recordings exceed the per-upload file limit (one is 339 MB), so videos get
    # the per-request ceiling (2 GB); pages, HAR and everything else stay at DOC_LIMIT_MB (reviewer #12).
    limit = max(settings.max_file_mb, settings.max_upload_mb) * 1024 * 1024
    skipped: list[dict[str, str]] = []
    planned, errors, dates = plan_incident(folder, limit, skipped, settings)
    doc_limit = max(settings.max_file_mb, DOC_LIMIT_MB) * 1024 * 1024
    for pa in [p for p in planned if p.size > doc_limit and not p.mime.startswith("video/")
               and p.role not in (ArtifactRole.SCREEN_RECORDING, ArtifactRole.AD_VIDEO)]:
        planned.remove(pa)
        errors.append({"path": pa.rel, "code": "FILE_TOO_LARGE"})
    for pa in planned:
        if roles and pa.rel in roles:
            pa.role = roles[pa.rel]
    sink = import_progress.current()  # IMPORT-PROGRESS: counters only, nothing below depends on it
    if sink is not None:
        top = [pa for pa in planned if pa.path is not None and "!/" not in pa.rel]
        top_err = sum(1 for e in errors if "!/" not in str(e.get("path", "")))
        sink.begin_incident(folder, len(top) + top_err, sum(pa.size for pa in top), len(errors) + len(skipped),
                            rejected_files=top_err)
    for pa in planned:
        _store(store, pa, limit)
        if sink is not None:
            sink.file_done(pa.rel, pa.size, top_level=pa.path is not None and "!/" not in pa.rel)
    # derived mhtml parts point to the stored mhtml digest
    by_rel = {pa.rel: pa.digest for pa in planned}
    for pa in planned:
        if "derived_from_path" in pa.meta:
            pa.meta["derived_from"] = by_rel.get(pa.meta["derived_from_path"])
    # explicit ids may merge on purpose (screenshot + its fetched landing page); derived ids never do: they are
    # resolved against source ownership in the same writer transaction as the incident write (CR-ASTRA-PIPE-102)
    iid = incident_id or incident_id_for(folder, prefix)
    counts = Counter(pa.role.value for pa in planned)
    page_roles = (ArtifactRole.LANDING_HTML, ArtifactRole.HAR)
    pages = sorted({pa.meta.get("page", "") for pa in planned if pa.role in page_roles})
    observed = min(dates) if dates else None
    artifacts = [
        IncidentArtifact(artifact_id=pa.digest or "", role=pa.role, path_in_incident=pa.rel, mime=pa.mime,
                         size=pa.size, meta=pa.meta)
        for pa in planned
    ]
    incident = Incident(
        id=iid, source=source, source_ref=source_ref_for(folder, settings), title=_title_guess(planned),
        observed_at=observed,
        artifacts=artifacts, label=label,
        meta={"role_counts": dict(counts), "pages": pages, "import_errors": errors, "files": len(planned),
              "skipped": skipped},
    )
    job_id = new_job_id()

    def _tx(conn: sqlite3.Connection) -> tuple[Incident, int]:
        inc = incident
        if incident_id is None:
            final = _unique_id(conn, iid, folder, inc.source_ref)
            if final != inc.id:
                inc = inc.model_copy(update={"id": final})
        return inc, _write_incident(conn, inc, job_id)

    incident, new = writer.run(_tx)
    if sink is not None:
        sink.end_incident()
    return ImportReport(incident, dict(counts), new, len(planned), sum(1 for p in planned if p.path is None), errors)


def _title_guess(planned: list[PlannedArtifact]) -> str | None:
    for pa in planned:
        if pa.role == ArtifactRole.LANDING_HTML and pa.path is not None:
            stem = pa.path.name.rsplit(".", 1)[0]
            if stem.lower() not in ("dom", "page", "index"):
                return redact_text(stem[:300])
    for pa in planned:
        if pa.role == ArtifactRole.PLATFORM_METADATA and pa.path is not None:
            try:
                d = json.loads(pa.path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(d, dict) and d.get("title"):
                    return redact_text(str(d["title"])[:300])
            except (ValueError, OSError):
                pass
    return None


def _write_incident(conn: sqlite3.Connection, inc: Incident, job_id: str) -> int:
    now = events.utcnow()
    prev = conn.execute("SELECT meta FROM incidents WHERE id = ?", (inc.id,)).fetchone()
    if prev is not None:  # keep the gene-cache fingerprint written by the pipeline
        old = json.loads(prev["meta"] or "{}")
        for k in ("genome_fp", "genes", "vectors", "extractor_seconds"):
            if k in old:
                inc.meta[k] = old[k]
    conn.execute(
        "INSERT INTO jobs (id, kind, status, params, total, created_at, started_at) VALUES (?, 'import_incident',"
        " 'running', ?, ?, ?, ?)",
        (job_id, events.canonical_json({"incident": inc.id, "source_ref": inc.source_ref}), len(inc.artifacts),
         now, now),
    )
    conn.execute(
        "INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET source=excluded.source,"
        " source_ref=excluded.source_ref, title=excluded.title, observed_at=excluded.observed_at,"
        " label=COALESCE(excluded.label, incidents.label), meta=excluded.meta",
        (inc.id, inc.source, inc.source_ref, inc.title, inc.observed_at, inc.label,
         events.canonical_json(inc.meta), now),
    )
    new = 0
    for idx, a in enumerate(inc.artifacts):
        is_new = register_artifact(conn, digest=a.artifact_id, mime=a.mime, size=a.size,
                                   source_ref=f"{inc.id}:{a.path_in_incident}", job_id=job_id, meta=a.meta)
        new += int(is_new)
        conn.execute(
            "INSERT OR IGNORE INTO incident_artifacts (incident_id, artifact_id, role, path_in_incident, meta)"
            " VALUES (?, ?, ?, ?, ?)",
            (inc.id, a.artifact_id, a.role, a.path_in_incident, events.canonical_json(a.meta)),
        )
        conn.execute(
            "INSERT INTO job_items (job_id, idx, source_ref, artifact_id, status) VALUES (?, ?, ?, ?, ?)",
            (job_id, idx, a.path_in_incident, a.artifact_id, "stored" if is_new else "duplicate"),
        )
    for j, err in enumerate(inc.meta.get("import_errors", [])):
        conn.execute(
            "INSERT INTO job_items (job_id, idx, source_ref, status, error_code) VALUES (?, ?, ?, 'quarantined', ?)",
            (job_id, len(inc.artifacts) + j, err["path"], err["code"]),
        )
    n_err = len(inc.meta.get("import_errors", []))
    conn.execute(
        "UPDATE jobs SET status='done', done=?, failed=?, total=?, finished_at=? WHERE id=?",
        (len(inc.artifacts), n_err, len(inc.artifacts) + n_err, events.utcnow(), job_id),
    )
    events.append(conn, "INCIDENT_IMPORTED", {"incident_id": inc.id, "artifacts": len(inc.artifacts),
                                              "new_artifacts": new, "job_id": job_id})
    return new


INCIDENT_FILE_EXT = IMAGE_EXT | VIDEO_EXT | {".html", ".htm", ".mhtml", ".mht", ".har", ".info.json"}
PAGE_UNIT_RE = re.compile(r"^(strona[_ -]?\d+|page[_ -]?\d+|.*_files|resources|assets)$", re.I)


def _is_evidence_file(f: Path) -> bool:
    """Media / page / network files make a folder an incident; README, PDF, CSV, notes do not."""
    return f.is_file() and f.name.lower() not in SKIP_NAMES and _ext(f.name) in INCIDENT_FILE_EXT


CAPTURE_MARKERS = {"manifest.json", "page.mhtml", "page.har", "dom.html"}
NUMBERED_RE = re.compile(r"^(\d+|(incident|incydent|case|sprawa|inc)[_ -]?\d+)([_-][\w-]{0,40})?$", re.I)


def _subdirs(path: Path) -> list[Path]:
    return [d for d in path.iterdir()
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and d.name not in SKIP_DIRS]


def _is_capture_bundle(d: Path, depth: int = 2) -> bool:
    """page-capture folder (`<host>_top/<host>_top/{manifest.json,page.mhtml,...}`)."""
    try:
        names = {f.name.lower() for f in d.iterdir() if f.is_file()}
    except OSError:
        return False
    if names & CAPTURE_MARKERS:
        return True
    subs = _subdirs(d) if depth > 0 else []
    return len(subs) == 1 and not names - SKIP_NAMES and _is_capture_bundle(subs[0], depth - 1)


def _is_page_unit(d: Path) -> bool:
    """A part of one incident (funnel stage, saved-page resources, capture bundle), never an incident itself."""
    if PAGE_UNIT_RE.match(d.name):
        return True
    return not NUMBERED_RE.match(d.name) and _is_capture_bundle(d)


def is_incident_folder(path: Path) -> bool:
    """Evidence files at the top level, or only page-unit subfolders (strona_N, *_files, capture bundles)."""
    subdirs = _subdirs(path)
    if not subdirs or all(_is_page_unit(d) for d in subdirs):
        return True
    files = [f for f in path.iterdir() if not f.name.startswith("._")]
    if any(_is_evidence_file(f) for f in files):
        incident_like = [d for d in subdirs if not _is_page_unit(d) and _has_evidence(d)]
        if any(NUMBERED_RE.match(d.name) for d in incident_like):
            return False  # numbered incident folders + loose material = dataset (loose files reported)
        return len(incident_like) < 2  # a dataset root with a few loose images is still a dataset
    # a chat log / PDF next to a single subfolder (e.g. `chat.txt` + `inner/incident.zip`): one incident, the
    # loose material is part of it, not a dataset README
    logs = [f for f in files if f.is_file() and f.suffix.lower() in LOOSE_DATA_EXT and f.name.lower() not in SKIP_NAMES
            and not f.name.lower().startswith("readme")]
    return bool(logs) and len(subdirs) == 1


LOOSE_DATA_EXT = {".txt", ".log", ".json", ".pdf", ".eml"}


def _has_evidence(d: Path, depth: int = 2) -> bool:
    try:
        for f in d.iterdir():
            if _is_evidence_file(f) or (depth > 0 and f.is_dir() and _has_evidence(f, depth - 1)):
                return True
    except OSError:
        return False
    return False


def _sort_key(d: Path) -> tuple:
    return (not d.name.isdigit(), int(d.name) if d.name.isdigit() else 0, d.name)


log = logging.getLogger(__name__)
REJECTED_ROOT_ARCHIVES: list[dict] = []  # codes of root-level incident ZIPs skipped by discover_incidents


def _wrapped_dataset(root: Path) -> Path | None:
    """The single subfolder of a wrapper folder (`root/<zbiór>/<incydenty>`) when `root` holds nothing else and that
    subfolder is itself a dataset, not one incident (JURY-READY: same rule as the wrapper of an unpacked ZIP)."""
    subs = _subdirs(root)
    if len(subs) != 1:
        return None
    files = [f for f in root.iterdir() if f.is_file() and not is_junk((f.name,))]
    if any(_is_evidence_file(f) or _is_archive(f) for f in files):
        return None
    return None if is_incident_folder(subs[0]) else subs[0]


def _archive_set(root: Path) -> bool:
    """A folder that holds only incident archives (>= 2 ZIPs, no subfolders, no evidence files): a ZIP of incident
    ZIPs. Used for dataset inputs only; a single item with inner ZIPs stays one incident (NESTED_ZIP_DEPTH)."""
    if _subdirs(root):
        return False
    files = [f for f in root.iterdir() if f.is_file() and not is_junk((f.name,))]
    archives = [f for f in files if _is_archive(f)]
    return len(archives) >= 2 and not any(_is_evidence_file(f) for f in files)


_DATASET_RULES: contextvars.ContextVar[bool] = contextvars.ContextVar("hydra_dataset_rules", default=False)


@contextmanager
def dataset_rules() -> Iterator[None]:
    """JURY-READY dataset rules for every `discover_incidents` call in this context (`hydra analyse`, GUI corpus
    upload via hydra.jury_report): wrapper folders are descended, a folder/ZIP of incident ZIPs is a dataset, a refused
    input archive is recorded in REJECTED_ROOT_ARCHIVES instead of raised. Off by default, so direct pipeline callers
    (evaluation harness) keep the previous discovery."""
    token = _DATASET_RULES.set(True)
    try:
        yield
    finally:
        _DATASET_RULES.reset(token)


def discover_incidents(root: Path, settings: Settings | None = None, *, dataset: bool | None = None) -> list[Path]:
    """`root` is one incident folder, a dataset folder with one subfolder per incident, a ZIP of either, or a
    single file. Loose files at a dataset root (README, PDF, …) never turn the dataset into one incident;
    they are listed by `loose_root_files`. ZIPs (the input, or incident ZIPs at a dataset root) are unpacked
    once into a content-addressed scratch folder, so re-imports see the same paths (idempotent).
    `dataset` (default: the `dataset_rules()` context): a wrapper folder holding one dataset folder is descended, a
    folder or ZIP holding only incident ZIPs is a dataset of those incidents, not one incident, and a refused input
    archive is recorded (REJECTED_ROOT_ARCHIVES) and yields no incident."""
    if dataset is None:
        dataset = _DATASET_RULES.get()
    root = root.expanduser().resolve()
    if root.is_file():
        if _is_archive(root):
            if not dataset:
                return discover_incidents(unpack_zip(root, settings), settings, dataset=dataset)
            try:
                unpacked = unpack_zip(root, settings)
            except ZipRejected as exc:
                REJECTED_ROOT_ARCHIVES.append({"path": root.name, "code": exc.code})
                log.warning("input archive %s rejected: %s", root.name, exc.code)
                return []
            return discover_incidents(unpacked, settings, dataset=dataset)
        return [root]
    inner = _wrapped_dataset(root) if dataset else None
    if inner is not None:
        return discover_incidents(inner, settings, dataset=dataset)
    if is_incident_folder(root) and not (dataset and _archive_set(root)):
        return [root]
    found = list(_subdirs(root))
    for f in sorted(root.iterdir()):
        if not (f.is_file() and _is_archive(f)):
            continue
        try:
            found.append(unpack_zip(f, settings))
        except ZipRejected as exc:
            # one rejected incident ZIP (bomb, too large, …) must not abort the whole dataset (CR-E02-1):
            # it is skipped with a logged code, the other incidents are analysed
            REJECTED_ROOT_ARCHIVES.append({"path": f.name, "code": exc.code})
            log.warning("incident archive %s rejected: %s", f.name, exc.code)
    return sorted(found, key=_sort_key)


def classify_input(path: Path, settings: Settings | None = None, *, dataset: bool | None = None) -> str:
    """`"dataset"` (several incidents), `"incident"` (one incident folder / incident ZIP) or `"file"` (one
    artifact). ZIPs are classified by their content. `dataset=True`: with the dataset rules of `discover_incidents`
    (a ZIP or folder of incident ZIPs is a dataset)."""
    path = Path(path).expanduser().resolve()
    if path.is_file() and not _is_archive(path):
        return "file"
    found = discover_incidents(path, settings, dataset=dataset)
    if len(found) == 1 and found[0].is_dir() and (path.is_file() or found[0] == path):
        return "incident"
    return "dataset"


def loose_root_files(root: Path) -> list[str]:
    """Files at a dataset root that are not part of any incident (reported, not imported)."""
    root = root.expanduser().resolve()
    if root.is_file() or is_incident_folder(root):
        return []
    return sorted(f.name for f in root.iterdir()
                  if f.is_file() and not is_junk((f.name,)) and not _is_archive(f))


# ------------------------------------------------------------------------------- ZIP inputs

ZIP_MAX_ENTRIES = 50_000
ZIP_MAX_RATIO = 200.0
DOCUMENT_ZIP_MARKERS = ("[Content_Types].xml", "mimetype")  # OOXML / ODF / EPUB are documents, not archives


def _is_archive(path: Path) -> bool:
    import zipfile

    if path.suffix.lower() not in (".zip", "") or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist()[:50])
    except (zipfile.BadZipFile, OSError):
        return False
    return not any(m in names for m in DOCUMENT_ZIP_MARKERS)


ZIP_MAX_TOTAL_MB = 8192      # total unpacked bytes of one archive
ZIP_FREE_MARGIN_MB = 512     # free space that must remain on the scratch volume after unpacking


class ZipRejected(ValueError):
    """The archive is not unpacked at all (bomb / too many entries / too big / no disk space)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _content_name(digest: str) -> str:
    return f"zip-{digest[:8]}"


def zip_content_name(path: Path) -> str:
    """Folder name of an unpacked archive whose files sit at the ZIP root (so its incident id is `nask-zip-<sha8>`,
    the same for any file name of the same bytes)."""
    return _content_name(sha256_file(path)[0])


def unpack_zip(path: Path, settings: Settings | None = None) -> Path:
    """Safe extraction of an incident or dataset ZIP → the folder to discover in (single wrapper folders such as
    `sample/` or `7/` are descended into). Path traversal, symlinks and OS junk are dropped; entry count,
    compression ratio, per-file ceiling, total unpacked bytes and free disk space are enforced. Dot-files that
    are data are kept (`.har`).

    The result depends only on the archive bytes: `<scratch>/unzip-<sha16>/zip-<sha8>/`, published by an atomic
    rename, so the same bytes under another file name, or two parallel imports, give the same folder."""
    import os
    import shutil
    import tempfile
    import uuid

    digest, _ = sha256_file(path)
    scratch = settings.tmp_dir if settings is not None else Path(tempfile.gettempdir()) / "hydra"
    scratch.mkdir(parents=True, exist_ok=True)
    base = scratch / f"unzip-{digest[:16]}"
    content = _content_name(digest)
    if not (base / content).is_dir():
        if base.exists():  # stale layout from an older build or a crashed run: never trusted
            shutil.rmtree(base, ignore_errors=True)
        work = scratch / f".unzip-{digest[:16]}.{uuid.uuid4().hex[:8]}"
        try:
            _extract_zip(path, work / content, settings, digest)
            try:
                os.rename(work, base)  # atomic publish; loses the race harmlessly
            except OSError:
                if not (base / content).is_dir():
                    raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
    out = base / content
    while True:  # descend single wrapper folders (`sample/`, `7/`), but never into a page unit
        entries = [e for e in out.iterdir() if not is_junk((e.name,)) and e.name not in SKIP_DIRS]
        if len(entries) == 1 and entries[0].is_dir() and not _is_page_unit(entries[0]):
            out = entries[0]
            continue
        return out


def zip_skipped(path: Path, settings: Settings | None = None) -> list[dict[str, str]]:
    """Entries `unpack_zip` dropped from this archive (traversal, symlink, too large, unreadable), from the
    `unpacked.json` of its content-addressed scratch folder; [] when not unpacked or unreadable."""
    import tempfile

    try:
        digest, _ = sha256_file(path)
        scratch = settings.tmp_dir if settings is not None else Path(tempfile.gettempdir()) / "hydra"
        info = json.loads((scratch / f"unzip-{digest[:16]}" / "unpacked.json").read_text(encoding="utf-8"))
        return [{"path": f"{path.name}!/{s.get('path', '')}", "code": str(s.get("code", ""))}
                for s in info.get("skipped", [])]
    except (OSError, ValueError):
        return []


def _extract_zip(path: Path, dest: Path, settings: Settings | None, digest: str) -> None:
    import shutil
    import zipfile
    from pathlib import PurePosixPath

    per_file = (max(settings.max_file_mb, settings.max_upload_mb) if settings else 2048) * MB
    max_entries = settings.max_zip_entries if settings else ZIP_MAX_ENTRIES
    max_ratio = settings.max_zip_ratio if settings else ZIP_MAX_RATIO
    skipped: list[dict[str, str]] = []
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > max_entries:
            raise ZipRejected("ZIP_TOO_MANY_ENTRIES", str(len(infos)))
        total = sum(i.file_size for i in infos)
        if total / max(1, sum(i.compress_size for i in infos)) > max_ratio:
            raise ZipRejected("ZIP_BOMB_SUSPECTED")
        if total > ZIP_MAX_TOTAL_MB * MB:
            raise ZipRejected("ZIP_TOO_LARGE", f"{total // MB} MB unpacked > {ZIP_MAX_TOTAL_MB} MB")
        dest.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(dest).free
        if total + ZIP_FREE_MARGIN_MB * MB > free:
            raise ZipRejected("ZIP_NO_SPACE", f"needs {total // MB} MB, {free // MB} MB free")
        written = 0
        for info in infos:
            # names without the UTF-8 flag are re-decoded (UTF-8 / cp852); every guard below runs on that name
            entry = zip_entry_name(info)
            name = PurePosixPath(entry.replace("\\", "/"))
            if not name.parts or name.is_absolute() or ".." in name.parts or ":" in name.parts[0]:
                skipped.append({"path": entry, "code": "ZIP_PATH_TRAVERSAL"})
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                skipped.append({"path": entry, "code": "ZIP_SYMLINK"})
                continue
            if is_junk(name.parts):
                continue
            if info.file_size > per_file:
                skipped.append({"path": entry, "code": "FILE_TOO_LARGE"})
                continue
            if zip_entry_encrypted(info):
                skipped.append({"path": entry, "code": "ZIP_ENTRY_ENCRYPTED"})
                continue
            target = dest.joinpath(*name.parts)
            try:
                # inside the entry boundary: an overlong or colliding name skips this entry only (CR-ASTRA-SEC-1)
                target.parent.mkdir(parents=True, exist_ok=True)
                n = 0
                with zf.open(info) as src, target.open("wb") as dst:
                    # the header sizes may lie: count real bytes against the declared size and the global cap
                    while chunk := src.read(1 << 20):
                        n += len(chunk)
                        if n > info.file_size or written + n > ZIP_MAX_TOTAL_MB * MB:
                            raise ZipRejected("ZIP_SIZE_MISMATCH", entry)
                        dst.write(chunk)
                written += n
            except ZipRejected:
                raise
            except Exception as exc:  # noqa: BLE001 — encrypted/unsupported entry: report, keep the rest
                with contextlib.suppress(OSError):
                    target.unlink(missing_ok=True)
                skipped.append({"path": entry, "code": f"ZIP_ENTRY_UNREADABLE:{type(exc).__name__}"})
    (dest.parent / "unpacked.json").write_text(
        json.dumps({"sha256": digest, "bytes": written, "skipped": skipped}), encoding="utf-8")


# ------------------------------------------------------------------------------- single inputs


def import_single(
    item: str | Path,
    writer: Writer,
    store: ContentStore,
    settings: Settings,
    *,
    incident_id: str | None = None,
    source: str = "upload",
    role: ArtifactRole | None = None,
) -> ImportReport:
    """One input (image, video, html/mhtml, har, text/log, folder, or a URL string) → a new incident."""
    import tempfile

    if isinstance(item, str) and re.match(r"^(https?://|www\.)", item.strip(), re.I):
        url = item.strip()
        tmpdir = Path(tempfile.mkdtemp(dir=settings.tmp_dir, prefix="url-"))
        (tmpdir / "input.url.txt").write_text(url + "\n", encoding="utf-8")
        iid = incident_id or f"new-{hashlib.sha256(url.encode()).hexdigest()[:8]}"
        rep = import_incident(tmpdir, writer, store, settings, incident_id=iid, source="url")
        return rep
    path = Path(item).expanduser().resolve()
    if path.is_dir():
        if incident_id is None:
            digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
            incident_id = f"new-{digest}"
        return import_incident(path, writer, store, settings, incident_id=incident_id, source=source)
    digest, _ = sha256_file(path)
    tmpdir = Path(tempfile.mkdtemp(dir=settings.tmp_dir, prefix="single-"))
    link = tmpdir / path.name
    try:
        link.hardlink_to(path)
    except OSError:
        import shutil

        shutil.copy2(path, link)
    return import_incident(tmpdir, writer, store, settings, incident_id=incident_id or f"new-{digest[:8]}",
                           source=source, roles={path.name: role} if role else None)


# ------------------------------------------------------------------------------- resolution


def load_incident(conn: sqlite3.Connection, incident_id: str) -> Incident | None:
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    if row is None:
        return None
    arts = []
    for r in conn.execute(
        "SELECT ia.artifact_id, ia.role, ia.path_in_incident, ia.meta, a.mime, a.size FROM incident_artifacts ia"
        " JOIN artifacts a ON a.id = ia.artifact_id WHERE ia.incident_id = ? ORDER BY ia.path_in_incident",
        (incident_id,),
    ):
        arts.append(IncidentArtifact(artifact_id=r["artifact_id"], role=r["role"],
                                     path_in_incident=r["path_in_incident"], mime=r["mime"], size=r["size"],
                                     meta=json.loads(r["meta"] or "{}")))
    return Incident(id=row["id"], source=row["source"], source_ref=row["source_ref"], title=row["title"],
                    observed_at=row["observed_at"], label=row["label"], meta=json.loads(row["meta"] or "{}"),
                    artifacts=arts)


def resolve(incident: Incident, store: ContentStore):
    """Incident → ResolvedIncident (each artifact mapped to its read-only original in the CAS)."""
    from hydra.genome.base import ResolvedArtifact, ResolvedIncident

    return ResolvedIncident(incident, [ResolvedArtifact(a, store.path_for(a.artifact_id)) for a in incident.artifacts
                                       if store.exists(a.artifact_id)])
