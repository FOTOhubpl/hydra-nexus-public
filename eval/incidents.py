"""Incident inputs for the evaluation harness.

An incident is a folder (NASK sample layout: creatives, recordings, saved pages + `_files/`, HAR, `.info.json`)
or a ZIP of such a folder. This module only *materialises* inputs (safe ZIP extraction, MHTML unpacking) and
classifies files into contract roles by content, not by extension, so renamed files still land in the right
role. Gene extraction lives in the backend (CORE pipeline, or the eval stub).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.ingest import sniff_mime

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_BYTES = 2 * 1024**3


@dataclass
class FileEntry:
    path: Path
    rel: str
    role: ArtifactRole
    mime: str
    size: int
    sha256: str


@dataclass
class IncidentFiles:
    incident_id: str
    root: Path
    files: list[FileEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_role(self, *roles: ArtifactRole) -> list[FileEntry]:
        want = {str(r) for r in roles}
        return [f for f in self.files if str(f.role) in want]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_like_har(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(4096).lstrip()
        if not head.startswith(b"{"):
            return False
        return b'"log"' in head[:2048]
    except OSError:
        return False


def _looks_like_mhtml(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(4096).lower()
    except OSError:
        return False
    return b"multipart/related" in head and (b"content-location" in head or b"mime-version" in head)


def classify(path: Path, rel: str) -> tuple[ArtifactRole, str]:
    """Role + sniffed MIME. Content first, then location (`_files/`), then name hints."""
    mime = sniff_mime(path)
    parts = PurePosixPath(rel).parts
    in_files_dir = any(p.endswith("_files") or p.endswith("-files") for p in parts[:-1])
    name = parts[-1].lower()
    if name.endswith(".info.json") or (mime == "application/json" and "info" in name and not _looks_like_har(path)):
        return ArtifactRole.PLATFORM_METADATA, mime
    if name.endswith(".har") or (mime in {"application/json", "text/plain"} and _looks_like_har(path)):
        return ArtifactRole.HAR, "application/json"
    if _looks_like_mhtml(path):
        return ArtifactRole.LANDING_HTML, "multipart/related"
    if in_files_dir:
        return ArtifactRole.LANDING_ASSET, mime
    if mime == "text/html":
        return ArtifactRole.LANDING_HTML, mime
    if mime.startswith("video/") or path.suffix.lower() in VIDEO_EXT:
        rec = any(k in name for k in ("screenrecording", "nagrywanie", "screen", "rec"))
        return (ArtifactRole.SCREEN_RECORDING if rec else ArtifactRole.AD_VIDEO), mime
    if mime.startswith("image/"):
        if any(k in name for k in ("zrzut", "screenshot", "img_", "screen")):
            return ArtifactRole.FEED_SCREENSHOT, mime
        return ArtifactRole.AD_CREATIVE, mime
    if mime == "application/json":
        return ArtifactRole.PLATFORM_METADATA, mime
    if mime.startswith("text/"):
        return ArtifactRole.TEXT, mime
    return ArtifactRole.OTHER, mime


def _safe_unzip(zip_path: Path, dest: Path) -> list[str]:
    warnings: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
            raise ValueError("zip too large for evaluation")
        for info in infos:
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or "__MACOSX" in name.parts:
                warnings.append(f"skipped unsafe zip entry {info.filename}")
                continue
            target = dest.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return warnings


def unpack_mhtml(path: Path, dest: Path) -> list[Path]:
    """MHTML → `page.html` + `page_files/<n>` so the same extractors apply. Returns written files."""
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    out: list[Path] = []
    files_dir = dest / f"{path.stem}_files"
    html_written = False
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        loc = part.get("Content-Location", "") or ""
        if ctype == "text/html" and not html_written:
            target = dest / f"{path.stem}.html"
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if loc:
                text = f"<!-- saved from url=({len(loc):04d}){loc} -->\n" + text
            target.write_text(text, encoding="utf-8")
            html_written = True
        else:
            files_dir.mkdir(parents=True, exist_ok=True)
            leaf = PurePosixPath(loc.split("?")[0]).name or f"part{i}"
            target = files_dir / f"{i:03d}_{leaf}"[:120]
            target.write_bytes(payload)
        out.append(target)
    return out


def materialise(src: Path, incident_id: str, scratch: Path | None = None) -> IncidentFiles:
    """Folder or ZIP (or a single file) → classified file list. MHTML is unpacked into scratch."""
    scratch = scratch or Path(tempfile.mkdtemp(prefix="hydra-eval-"))
    warnings: list[str] = []
    if src.is_file() and zipfile.is_zipfile(src):
        root = scratch / f"zip-{incident_id}"
        root.mkdir(parents=True, exist_ok=True)
        warnings += _safe_unzip(src, root)
        kids = [p for p in root.iterdir() if not p.name.startswith("__")]
        if len(kids) == 1 and kids[0].is_dir():
            root = kids[0]
    elif src.is_file():
        root = scratch / f"file-{incident_id}"
        root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, root / src.name)
    else:
        root = src
    inc = IncidentFiles(incident_id=incident_id, root=root, warnings=warnings)
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".DS_Store")
    for p in paths:
        rel = p.relative_to(root).as_posix()
        try:
            role, mime = classify(p, rel)
            if role == ArtifactRole.LANDING_HTML and mime == "multipart/related":
                mdir = scratch / f"mhtml-{incident_id}-{hashlib.sha1(rel.encode()).hexdigest()[:8]}"
                mdir.mkdir(parents=True, exist_ok=True)
                for q in unpack_mhtml(p, mdir):
                    qrel = f"{rel}!/{q.relative_to(mdir).as_posix()}"
                    qrole, qmime = classify(q, q.relative_to(mdir).as_posix())
                    inc.files.append(FileEntry(q, qrel, qrole, qmime, q.stat().st_size, _sha256(q)))
                continue
            inc.files.append(FileEntry(p, rel, role, mime, p.stat().st_size, _sha256(p)))
        except Exception as exc:  # noqa: BLE001 — one unreadable file never stops the incident
            warnings.append(f"{rel}: {type(exc).__name__}: {exc}"[:300])
    return inc


def to_contract(inc: IncidentFiles, source: str, label: str | None = None) -> Incident:
    arts = [
        IncidentArtifact(artifact_id=f.sha256, role=f.role, path_in_incident=f.rel, mime=f.mime, size=f.size)
        for f in inc.files
    ]
    return Incident(id=inc.incident_id, source=source, source_ref=str(inc.root), artifacts=arts, label=label,
                    meta={"warnings": inc.warnings} if inc.warnings else {})


def load_info_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
