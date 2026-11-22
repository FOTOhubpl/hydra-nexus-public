"""Generic safe import: files, folders, ZIP archives → content-addressed originals + registry rows.

Guarantees (spec acceptance "Import"): every input gets a final status in `job_items`
(stored | duplicate | quarantined | failed), nothing disappears silently, a bad file never stops
the queue, re-import is idempotent (same bytes → same artifact id), originals stay byte-identical.

Data-format adapters for the organiser's dataset are task logic and are written from Sat 11:00
(`hydra/adapters/`); they call `import_paths` or `register_artifact` with extra metadata.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import filetype

from hydra import events
from hydra.config import Settings
from hydra.jobs import JobContext
from hydra.paths import allowed_roots, is_within, safe_display_name
from hydra.storage import ContentStore

# Generic allow-list by sniffed MIME (not by extension). Everything else is quarantined.
ALLOWED_PREFIXES = ("image/", "video/", "audio/", "text/")
ALLOWED_EXACT = {"application/json", "application/pdf", "application/xml"}
TEXT_EXTENSIONS = {".txt", ".csv", ".tsv", ".json", ".jsonl", ".html", ".htm", ".xml", ".md", ".srt", ".vtt"}


class QuarantineError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


SNIFF_BYTES = 64 * 1024


def _decode_text(head: bytes, ext: str, truncated: bool) -> str | None:
    """UTF-8 (tolerating a multibyte char cut at the read boundary); cp1250 for known text extensions."""
    if b"\x00" in head:
        return None
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated and exc.start >= len(head) - 3 and exc.reason == "unexpected end of data":
            return head[: exc.start].decode("utf-8", errors="strict")
    if ext in TEXT_EXTENSIONS:
        try:
            return head.decode("cp1250")  # Polish Windows exports (CSV/TXT)
        except UnicodeDecodeError:
            return None
    return None


def _looks_like_json(text: str, truncated: bool) -> bool:
    """True when text starting with `{`/`[` is JSON (or JSON Lines), not a chat export `[01.10.2026, 10:00] ...`.

    A complete head must parse (as one document or line by line). A head cut at SNIFF_BYTES cannot parse, so it is
    accepted when the decoder fails only in the second half of the text (truncation), not at the first tokens.
    """
    body = text.strip()
    try:
        json.loads(body)
        return True
    except ValueError as exc:
        pos = exc.pos if isinstance(exc, json.JSONDecodeError) else 0
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) > 1:
        try:
            for ln in lines[: len(lines) - 1] if truncated else lines:
                json.loads(ln)
            return True
        except ValueError:
            pass
    return truncated and pos >= len(body) // 2


def sniff_mime(path: Path) -> str:
    """MIME from magic bytes; text formats (no magic) by UTF-8/cp1250 decodability + extension."""
    with path.open("rb") as fh:
        return sniff_head(fh.read(SNIFF_BYTES + 1), path.suffix)


def sniff_head(head: bytes, suffix: str) -> str:
    """`sniff_mime` on the first SNIFF_BYTES + 1 bytes of a file (lets a caller sniff an already-open handle)."""
    kind = filetype.guess(head) if head else None
    if kind is not None:
        return kind.mime
    truncated = len(head) > SNIFF_BYTES
    head = head[:SNIFF_BYTES]
    ext = suffix.lower()
    try:
        text = _decode_text(head, ext, truncated)
    except UnicodeDecodeError:
        text = None
    if text is None:
        return "application/octet-stream"
    stripped = text.lstrip().lower()
    if ext in {".html", ".htm"} or stripped.startswith(("<!doctype html", "<html")):
        return "text/html"
    if ext in {".json", ".jsonl"} or (stripped.startswith(("{", "[")) and _looks_like_json(text, truncated)):
        return "application/json"
    if ext == ".xml" or stripped.startswith("<?xml"):
        return "application/xml"
    if ext == ".csv":
        return "text/csv"
    return "text/plain"


def is_allowed(mime: str) -> bool:
    return mime.startswith(ALLOWED_PREFIXES) or mime in ALLOWED_EXACT


@dataclass
class InputFile:
    path: Path
    source_ref: str
    # Non-empty for files found by walking a folder: the real path must stay under one of these when read.
    roots: tuple[Path, ...] = ()


# ZIP containers that are documents, not archives (OOXML, ODF, EPUB): stored whole, never exploded.
_DOCUMENT_ZIP_MARKERS = ("[Content_Types].xml", "mimetype")


def _is_document_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist()[:50])
    except Exception:  # noqa: BLE001 — unreadable archives are handled by the caller
        return False
    return any(m in names for m in _DOCUMENT_ZIP_MARKERS)


Skipped = tuple[str, str, str]  # (source_ref, error_code, detail)

NAME_MAX_BYTES = 255  # one path component (ext4/APFS/NTFS limit)
PATH_MAX_BYTES = 4096
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # a FIFO swapped in must never block the import worker


_ZIP_UTF8_FLAG = 0x800  # general purpose bit 11 (EFS): the entry name is UTF-8
_ZIP_ENCRYPTED_FLAG = 0x1
_POLISH_LETTERS = frozenset("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
ZIP_ENTRY_ENCRYPTED_PL = "Wpis archiwum jest zaszyfrowany hasłem: nie jest rozpakowywany."


def zip_entry_name(info: zipfile.ZipInfo) -> str:
    """The display name of a ZIP entry. Python decodes names without the UTF-8 flag (bit 11) as cp437, but Info-ZIP
    `zip` on macOS/Linux writes raw UTF-8 bytes without the flag and Windows Explorer (Polish locale) writes cp852.
    The raw bytes are recovered (cp437 maps every byte, so the round trip is exact) and re-decoded: strict UTF-8
    first, then cp852 when the result reads as Polish text, else the cp437 name is kept. The caller must validate
    the returned name (path traversal, symlink, length) exactly as it would the original one."""
    name = info.filename
    if info.flag_bits & _ZIP_UTF8_FLAG or name.isascii():
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:  # zipfile was given another metadata encoding: trust it
        return name
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    pl = raw.decode("cp852")
    extra = [c for c in pl if not c.isascii()]
    if extra and all(c.isalpha() for c in extra) and any(c in _POLISH_LETTERS for c in extra):
        return pl
    return name


def zip_entry_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & _ZIP_ENCRYPTED_FLAG)


def _entry_problem(info: zipfile.ZipInfo, settings: Settings, raw: str | None = None) -> tuple[str, str] | None:
    """Why a ZIP entry is not extracted, as (error_code, detail), or None. Checked per entry, never fatal.
    `raw` is the decoded entry name (`zip_entry_name`); every check runs on it."""
    raw = info.filename if raw is None else raw
    name = PurePosixPath(raw.replace("\\", "/"))
    if name.is_absolute() or ".." in name.parts or (name.parts and ":" in name.parts[0]):
        return "ZIP_PATH_TRAVERSAL", "entry escapes the archive root"
    if stat.S_ISLNK(info.external_attr >> 16):
        # never materialised: neither as a filesystem link nor as a file holding the link target
        return "ZIP_SYMLINK", "Dowiązanie symboliczne w archiwum: nie jest rozpakowywane ani śledzone."
    encoded = raw.encode("utf-8", "surrogatepass")
    if len(encoded) > PATH_MAX_BYTES or any(len(p.encode("utf-8", "surrogatepass")) > NAME_MAX_BYTES
                                            for p in name.parts):
        return "ZIP_NAME_TOO_LONG", (f"Nazwa wpisu w archiwum jest zbyt długa (składnik > {NAME_MAX_BYTES} bajtów"
                                     f" lub ścieżka > {PATH_MAX_BYTES} bajtów).")
    if name.name.startswith(".") or any(part.startswith(".") or part == "__MACOSX" for part in name.parts):
        return "HIDDEN_ENTRY", "hidden or OS metadata entry"
    if info.file_size > settings.max_file_mb * 1024 * 1024:
        return "FILE_TOO_LARGE", f"> {settings.max_file_mb} MB"
    return None


def _safe_extract(zip_path: Path, dest: Path, settings: Settings) -> list[InputFile | Skipped]:
    """Archive-wide limits raise QuarantineError; everything else is decided per entry (CR-ASTRA-SEC-1).

    Entries are materialised flat as `<dest>/<index><suffix>` (exclusive create), never under their archive
    names, so long, colliding, case-folded or control-character names cannot break siblings; the archive name
    is kept only as the source reference."""
    out: list[InputFile | Skipped] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > settings.max_zip_entries:
            raise QuarantineError("ZIP_TOO_MANY_ENTRIES", f"{len(infos)} entries")
        total_unc = sum(i.file_size for i in infos)
        total_comp = max(1, sum(i.compress_size for i in infos))
        if total_unc / total_comp > settings.max_zip_ratio:
            raise QuarantineError("ZIP_BOMB_SUSPECTED", f"ratio {total_unc / total_comp:.0f}")
        for n, info in enumerate(infos):
            entry = zip_entry_name(info)
            ref = f"{zip_path.name}!/{entry}"
            target: Path | None = None
            try:
                problem = _entry_problem(info, settings, entry)
                if problem is not None:  # never written to disk, but always reported (nothing disappears silently)
                    out.append((ref, *problem))
                    continue
                if zip_entry_encrypted(info):
                    out.append((ref, "ZIP_ENTRY_ENCRYPTED", ZIP_ENTRY_ENCRYPTED_PL))
                    continue
                suffix = PurePosixPath(entry.replace("\\", "/")).suffix
                target = dest / f"{n:06d}{suffix if _SAFE_SUFFIX.fullmatch(suffix) else ''}"
                with zf.open(info) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
            except Exception as exc:  # noqa: BLE001 — encrypted/unsupported/odd entry: report it, keep the rest
                if target is not None:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass  # the scratch dir is removed as a whole after the import
                out.append((ref, "ZIP_ENTRY_UNREADABLE", f"{type(exc).__name__}: {exc}"[:500]))
                continue
            out.append(InputFile(target, ref, roots=(dest.resolve(),)))
    return out


def _folder_entry(f: Path, ref: str, roots: tuple[Path, ...]) -> InputFile | Skipped:
    """One file found while walking a folder. A child symlink is used only when its real path stays inside
    the walked folder or the allowed roots (CR-ASTRA-SEC-6); special files are reported, never opened."""
    try:
        st = f.lstat()
    except OSError as exc:
        return (ref, "FILE_UNREADABLE", f"{type(exc).__name__}: {exc}"[:500])
    if stat.S_ISLNK(st.st_mode):
        try:
            real = f.resolve(strict=True)
        except (OSError, RuntimeError):
            return (ref, "PATH_NOT_ALLOWED", "Dowiązanie symboliczne jest zerwane lub zapętlone; plik pominięty.")
        if not is_within(real, list(roots)):
            return (ref, "PATH_NOT_ALLOWED", "Dowiązanie symboliczne prowadzi poza dozwolony katalog; plik pominięty.")
        if real.is_dir():
            return (ref, "SYMLINK_DIR_NOT_FOLLOWED", "Dowiązanie symboliczne do katalogu nie jest śledzone.")
        if not real.is_file():
            return (ref, "NOT_REGULAR_FILE", "To nie jest zwykły plik (urządzenie, potok lub gniazdo).")
        return InputFile(real, ref, roots=roots)
    if not stat.S_ISREG(st.st_mode):
        return (ref, "NOT_REGULAR_FILE", "To nie jest zwykły plik (urządzenie, potok lub gniazdo).")
    return InputFile(f, ref, roots=roots)


def _walk_folder(p: Path, settings: Settings) -> Iterator[InputFile | Skipped]:
    """Sorted files under `p`; hidden names skipped, symlinked directories reported and never entered."""
    root = p.resolve()
    try:
        roots = (root, *allowed_roots(settings))
    except AssertionError:  # no data_dir configured: confine to the walked folder itself
        roots = (root,)
    found: list[tuple[Path, InputFile | Skipped]] = []

    def unreadable(exc: OSError) -> None:
        where = Path(exc.filename) if exc.filename else p
        ref = str(where.relative_to(p.parent)) if where.is_relative_to(p.parent) else str(where)
        found.append((where, (ref, "DIR_UNREADABLE", f"{type(exc).__name__}: {exc.strerror}"[:500])))

    for dirpath, dirnames, filenames in os.walk(p, onerror=unreadable, followlinks=False):
        d = Path(dirpath)
        keep = []
        for name in dirnames:
            if name.startswith("."):
                continue
            if (d / name).is_symlink():
                found.append((d / name, (str((d / name).relative_to(p.parent)), "SYMLINK_DIR_NOT_FOLLOWED",
                                         "Dowiązanie symboliczne do katalogu nie jest śledzone.")))
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if not name.startswith("."):
                f = d / name
                found.append((f, _folder_entry(f, str(f.relative_to(p.parent)), roots)))
    for _, entry in sorted(found, key=lambda t: t[0]):
        yield entry


def _open_input(item: InputFile) -> BinaryIO:
    """Open one input for reading. Confined items (folder children, ZIP extracts) are re-checked against their
    roots right before the read and opened with O_NOFOLLOW; non-regular files are refused without blocking."""
    flags = os.O_RDONLY | _NONBLOCK
    if item.roots:
        if not is_within(item.path.resolve(), list(item.roots)):
            raise QuarantineError("PATH_NOT_ALLOWED", "Plik prowadzi poza dozwolony katalog; pominięty.")
        flags |= _NOFOLLOW
    try:
        fd = os.open(item.path, flags)
    except OSError as exc:
        if item.roots and item.path.is_symlink():
            raise QuarantineError("PATH_NOT_ALLOWED", "Dowiązanie symboliczne nie jest śledzone.") from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise QuarantineError("NOT_REGULAR_FILE", "To nie jest zwykły plik (urządzenie, potok lub gniazdo).")
    return os.fdopen(fd, "rb")


def _expand_one(p: Path, scratch: Path, settings: Settings) -> Iterator[InputFile | Skipped]:
    if p.is_dir():
        yield from _walk_folder(p, settings)
    elif p.is_file() and zipfile.is_zipfile(p) and not _is_document_zip(p):
        try:
            yield from _safe_extract(p, Path(tempfile.mkdtemp(dir=scratch, prefix="zip-")), settings)
        except QuarantineError as exc:
            yield (p.name, exc.code, exc.detail)
        except Exception as exc:  # noqa: BLE001 — encrypted, deflate64, corrupt: one bad ZIP never fails the job
            yield (p.name, "ZIP_UNREADABLE", f"{type(exc).__name__}: {exc}"[:500])
    elif p.is_file():
        yield InputFile(p, p.name)
    else:
        yield (str(p), "NOT_FOUND", "path does not exist")


def expand_inputs(paths: list[Path], scratch: Path, settings: Settings) -> Iterator[InputFile | Skipped]:
    """Yields InputFile, or (source_ref, error_code, detail) for inputs that cannot be expanded.

    Each top-level input is expanded completely before it is yielded: an error inside one input (unreadable
    folder, overlong path) becomes one INPUT_UNREADABLE status and never discards the other inputs."""
    for p in paths:
        try:
            entries = list(_expand_one(p, scratch, settings))
        except Exception as exc:  # noqa: BLE001 — one bad input never fails the job
            entries = [(str(p), "INPUT_UNREADABLE", f"Nie można odczytać wejścia: {type(exc).__name__}: {exc}"[:500])]
        yield from entries


def register_artifact(
    conn: sqlite3.Connection,
    *,
    digest: str,
    mime: str,
    size: int,
    source_ref: str,
    job_id: str,
    observed_at: str | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Insert the artifact if new and always record this source. Returns True when new."""
    now = events.utcnow()
    cur = conn.execute(
        "INSERT OR IGNORE INTO artifacts (id, mime, size, source_ref, observed_at, meta, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (digest, mime, size, source_ref, observed_at, events.canonical_json(meta or {}), now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO artifact_sources (artifact_id, source_ref, job_id, imported_at) VALUES (?, ?, ?, ?)",
        (digest, source_ref, job_id, now),
    )
    is_new = cur.rowcount == 1
    if is_new:
        events.append(conn, "ARTIFACT_STORED", {"artifact_id": digest, "mime": mime, "size": size})
    return is_new


def import_paths(ctx: JobContext, paths: list[Path], settings: Settings, store: ContentStore) -> None:
    settings.ensure_dirs()
    scratch = Path(tempfile.mkdtemp(dir=settings.tmp_dir, prefix=f"import-{ctx.job_id}-"))
    try:
        items = list(expand_inputs(paths, scratch, settings))
        ctx.set_total(len(items))
        limit = settings.max_file_mb * 1024 * 1024
        for idx, item in enumerate(items):
            if isinstance(item, tuple):
                ref, code, detail = item
                ctx.record_item(idx, safe_display_name(ref), "quarantined", error_code=code,
                                detail=safe_display_name(detail))
                continue
            ref = safe_display_name(item.source_ref)
            try:
                with _open_input(item) as fh:
                    size = os.fstat(fh.fileno()).st_size
                    if size > limit:
                        raise QuarantineError("FILE_TOO_LARGE", f"> {settings.max_file_mb} MB")
                    if size == 0:
                        raise QuarantineError("FILE_EMPTY", "0 bytes")
                    mime = sniff_head(fh.read(SNIFF_BYTES + 1), item.path.suffix)
                    if not is_allowed(mime):
                        raise QuarantineError("MIME_NOT_ALLOWED", mime)
                    fh.seek(0)
                    digest, size = store.put_stream(fh, max_bytes=limit)

                def _tx(conn: sqlite3.Connection, d=digest, m=mime, s=size, r=ref) -> str | None:
                    is_new = register_artifact(conn, digest=d, mime=m, size=s, source_ref=r, job_id=ctx.job_id)
                    return None if is_new else "duplicate"

                # artifact row + source + item status commit in ONE transaction
                ctx.record_item(idx, ref, "stored", artifact_id=digest, extra_tx=_tx)
            except QuarantineError as exc:
                ctx.record_item(idx, ref, "quarantined", error_code=exc.code, detail=safe_display_name(exc.detail))
            except Exception as exc:  # noqa: BLE001 — per-file failure must not stop the queue
                ctx.record_item(idx, ref, "failed", error_code=type(exc).__name__,
                                detail=safe_display_name(str(exc)[:500]))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
