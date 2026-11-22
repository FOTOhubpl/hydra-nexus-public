"""Content-addressed store (CAS): originals are stored once, by SHA-256, read-only.

Layout: <cas>/<aa>/<bb>/<sha256>. Writes are atomic (temp file + rename), so a crash never
leaves a half-written original under its final name.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

CHUNK = 1 << 20
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class UnsafeStorePath(FileNotFoundError):
    """A CAS entry that is missing, not a regular file, a symlink, or resolves outside the store root.

    Subclasses FileNotFoundError so callers that already map a missing original to 404 stay correct."""


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


class ContentStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"not a sha256 hex digest: {digest!r}")
        return self.root / digest[:2] / digest[2:4] / digest

    def safe_path(self, digest: str) -> Path:
        """`path_for` for reading or serving: refuses symlinks and anything whose realpath leaves the store.

        Raises UnsafeStorePath (a FileNotFoundError). Use this, or `open`, wherever CAS bytes are served or
        previewed; `path_for` alone trusts the filesystem layout (CR-ASTRA-SEC-6)."""
        path = self.path_for(digest)
        try:
            st = path.lstat()
        except OSError as exc:
            raise UnsafeStorePath(f"original missing from the store: {digest}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeStorePath(f"store entry is not a regular file: {digest}")
        root = self.root.resolve()
        if not path.resolve().is_relative_to(root):
            raise UnsafeStorePath(f"store entry resolves outside the store: {digest}")
        return path

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def _commit(self, tmp: Path, digest: str) -> Path:
        final = self.path_for(digest)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.chmod(tmp, 0o444)
            os.replace(tmp, final)
        return final

    def put_stream(self, src: BinaryIO, max_bytes: int | None = None) -> tuple[str, int]:
        """Stream into the store. Raises ValueError when max_bytes is exceeded."""
        h = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".incoming-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := src.read(CHUNK):
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise ValueError(f"file exceeds limit of {max_bytes} bytes")
                    h.update(chunk)
                    out.write(chunk)
            digest = h.hexdigest()
            self._commit(tmp, digest)
            return digest, size
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def put_file(self, path: Path, max_bytes: int | None = None) -> tuple[str, int]:
        with path.open("rb") as f:
            return self.put_stream(f, max_bytes=max_bytes)

    def put_bytes(self, data: bytes) -> str:
        import io

        digest, _ = self.put_stream(io.BytesIO(data))
        return digest

    def open(self, digest: str) -> BinaryIO:
        """Open an original for reading; never follows a symlink at the final component (O_NOFOLLOW)."""
        path = self.safe_path(digest)
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except OSError as exc:
            raise UnsafeStorePath(f"store entry cannot be opened safely: {digest}") from exc
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise UnsafeStorePath(f"store entry is not a regular file: {digest}")
        return os.fdopen(fd, "rb")

    def read_bytes(self, digest: str) -> bytes:
        with self.open(digest) as fh:
            return fh.read()

    def verify(self, digest: str) -> bool:
        """Re-hash a stored original; False means the bytes on disk changed."""
        actual, _ = sha256_file(self.path_for(digest))
        return actual == digest

    def iter_digests(self) -> Iterator[str]:
        for p in self.root.glob("??/??/*"):
            if p.is_file() and not p.name.startswith("."):
                yield p.name
