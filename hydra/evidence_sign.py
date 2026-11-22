"""Signed evidence package: Ed25519 signature over a canonical manifest of every file (spec 3.0 M8, test 7).

`sign_package(settings, folder, run_id)` writes two files into an export folder:

- `SIGNED_MANIFEST.json`: `hydra.events.canonical_json` of {schema, every file path + sha256 + size, run_id,
  product version + commit, audit chain head (seq + hash), creation time, the Ed25519 public key and its
  fingerprint}. The bytes on disk are exactly the signed bytes.
- `SIGNED_MANIFEST.sig`: base64 of the 64-byte Ed25519 signature of those bytes.

`verify_package(path)` accepts the folder or a ZIP of it (read in memory, never extracted) and checks: the
signature against the embedded public key (and optionally a pinned fingerprint), canonical form of the manifest,
every file hash and size, no missing and no extra files, no unsafe member names (absolute, `..`, backslash, drive,
control characters, symlinks). The signature proves integrity since signing, not truth of the material.

The key is generated per installation on first use at `<HYDRA_STATE_DIR>/keys/evidence_ed25519.pem` (mode 0600,
directory 0700). Offline only: no key server, no timestamp authority.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hydra import __version__
from hydra.config import Settings
from hydra.events import canonical_json, utcnow

SCHEMA = "hydra.evidence.signed_manifest.v1"
MANIFEST_NAME = "SIGNED_MANIFEST.json"
SIG_NAME = "SIGNED_MANIFEST.sig"
SIGNATURE_FILES = frozenset({MANIFEST_NAME, SIG_NAME})
KEY_NAME = "evidence_ed25519.pem"
MAX_VERIFY_BYTES = 4 * 1024**3  # refuse to hash more than 4 GiB of declared content
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\\:]")


# ----------------------------------------------------------------------------------------------- keys


def key_path(settings: Settings) -> Path:
    assert settings.state_dir is not None
    return Path(settings.state_dir) / "keys" / KEY_NAME


def load_or_create_key(settings: Settings) -> Ed25519PrivateKey:
    """The installation's signing key; created on first use (0600 file in a 0700 directory)."""
    p = key_path(settings)
    if p.is_file():
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{p} is not an Ed25519 private key")
        return key
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process created it in between: use theirs
        return load_or_create_key(settings)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def public_raw(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(pub: Ed25519PublicKey | bytes) -> str:
    """`SHA256:<hex>` of the raw 32-byte public key (shown in the UI and the CLI, compare it out of band)."""
    raw = pub if isinstance(pub, bytes) else public_raw(pub)
    return "SHA256:" + hashlib.sha256(raw).hexdigest()


def key_info(settings: Settings, create: bool = True) -> dict[str, Any]:
    if not create and not key_path(settings).is_file():
        return {"algorithm": "Ed25519", "fingerprint": None, "public_key": None}
    pub = load_or_create_key(settings).public_key()
    return {"algorithm": "Ed25519", "fingerprint": fingerprint(pub),
            "public_key": base64.b64encode(public_raw(pub)).decode()}


# ---------------------------------------------------------------------------------------------- sign


def _audit_head(settings: Settings) -> dict[str, Any] | None:
    from hydra.db import connect

    if not settings.db_path.exists():
        return None
    try:
        with closing(connect(settings.db_path, readonly=True)) as conn:
            row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return {"seq": int(row["seq"]), "hash": row["hash"]} if row else {"seq": 0, "hash": "0" * 64}


def _product_commit() -> str | None:
    from hydra.train import _git_commit

    return os.environ.get("HYDRA_PRODUCT_COMMIT") or _git_commit()


def _walk(folder: Path) -> list[str]:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_symlink():
            raise ValueError(f"symlink in package: {p.relative_to(folder).as_posix()}")
        if p.is_file():
            rel = p.relative_to(folder).as_posix()
            if rel not in SIGNATURE_FILES:
                out.append(rel)
    return out


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sign_package(settings: Settings, folder: Path, run_id: str | None) -> dict[str, Any]:
    """Write SIGNED_MANIFEST.json + SIGNED_MANIFEST.sig into `folder`; returns the manifest."""
    folder = Path(folder)
    key = load_or_create_key(settings)
    pub = public_raw(key.public_key())
    files = []
    for rel in _walk(folder):
        if not safe_member(rel):
            raise ValueError(f"unsafe file name in package: {rel!r}")
        p = folder / rel
        files.append({"path": rel, "sha256": _sha256_file(p), "size": p.stat().st_size})
    manifest = {
        "schema": SCHEMA, "algorithm": "Ed25519", "created_at": utcnow(), "run_id": run_id,
        "product": {"name": "hydra-nexus", "version": __version__, "commit": _product_commit()},
        "audit_chain_head": _audit_head(settings), "files": files,
        "public_key": base64.b64encode(pub).decode(), "key_fingerprint": fingerprint(pub),
        "note": "Podpis potwierdza integralność od chwili podpisania, nie prawdziwość materiału."
                " / The signature proves integrity since signing, not truth of the material.",
    }
    body = canonical_json(manifest).encode("utf-8")
    (folder / MANIFEST_NAME).write_bytes(body)
    (folder / SIG_NAME).write_bytes(base64.b64encode(key.sign(body)) + b"\n")
    return manifest


# -------------------------------------------------------------------------------------------- verify


def safe_member(name: str) -> bool:
    """Relative POSIX path without `..`, empty parts, absolute roots, drives, backslashes or control chars."""
    if not name or name.startswith("/") or _UNSAFE.search(name):
        return False
    return all(p not in ("", ".", "..") for p in name.split("/"))


class _Source:
    """Uniform read access to a package folder or ZIP (ZIP is never extracted)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.zip: zipfile.ZipFile | None = None
        self.prefix = ""
        self.errors: list[str] = []
        self.names: list[str] = []
        if self.path.is_dir():
            try:
                self.names = _walk(self.path) + [n for n in sorted(SIGNATURE_FILES) if (self.path / n).is_file()]
            except ValueError as exc:
                self.errors.append(str(exc))
        elif zipfile.is_zipfile(self.path):
            self.zip = zipfile.ZipFile(self.path)
            raw = [i for i in self.zip.infolist() if not i.is_dir()]
            for i in raw:
                if not safe_member(i.filename):
                    self.errors.append(f"niebezpieczna nazwa w ZIP / unsafe ZIP member name: {i.filename!r}")
                elif (i.external_attr >> 16) & 0o170000 == 0o120000:
                    self.errors.append(f"dowiązanie symboliczne w ZIP / symlink in ZIP: {i.filename!r}")
            names = [i.filename for i in raw if safe_member(i.filename)]
            if len(names) != len(set(names)):
                self.errors.append("powtórzone nazwy w ZIP / duplicate ZIP member names")
            roots = [n[: -len(MANIFEST_NAME)] for n in names if n == MANIFEST_NAME or n.endswith("/" + MANIFEST_NAME)]
            if len(roots) == 1:
                self.prefix = roots[0]
            elif len(roots) > 1:
                self.errors.append(f"wiele manifestów w ZIP / several manifests in ZIP: {len(roots)}")
            for n in names:
                if n.startswith(self.prefix):
                    self.names.append(n[len(self.prefix):])
                else:
                    self.errors.append(f"plik poza katalogiem pakietu / file outside package root: {n!r}")
        else:
            self.errors.append(f"to nie jest katalog ani ZIP / not a folder or ZIP: {self.path.name}")

    def read(self, rel: str) -> bytes:
        if self.zip is not None:
            return self.zip.read(self.prefix + rel)
        return (self.path / rel).read_bytes()

    def digest(self, rel: str, limit: int) -> tuple[str, int]:
        """sha256 and size, reading at most `limit + 1` bytes (a ZIP bomb cannot outgrow its declared size)."""
        h = hashlib.sha256()
        n = 0
        f = self.zip.open(self.prefix + rel) if self.zip is not None else (self.path / rel).open("rb")
        with f:
            while n <= limit:
                chunk = f.read(min(1 << 20, limit + 1 - n))
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
        return h.hexdigest(), n

    def close(self) -> None:
        if self.zip is not None:
            self.zip.close()


def verify_package(path: Path, expected_fingerprint: str | None = None) -> dict[str, Any]:
    """Returns {ok, errors[], files_checked, run_id, key_fingerprint, ...}. Never raises on a bad package."""
    out: dict[str, Any] = {"package": str(path), "ok": False, "errors": [], "files_checked": 0,
                           "run_id": None, "key_fingerprint": None, "audit_chain_head": None, "product": None}
    try:
        src = _Source(Path(path))
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        out["errors"].append(f"błąd odczytu / read error: {exc}")
        return out
    errors = out["errors"]
    errors.extend(src.errors)
    try:
        names = set(src.names)
        if MANIFEST_NAME not in names or SIG_NAME not in names:
            errors.append(f"brak {MANIFEST_NAME} lub {SIG_NAME} / signed manifest or signature missing")
            return out
        body = src.read(MANIFEST_NAME)
        try:
            manifest = json.loads(body.decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            errors.append(f"manifest nieczytelny / manifest unreadable: {exc}")
            return out
        if canonical_json(manifest).encode("utf-8") != body:
            errors.append("manifest nie jest w postaci kanonicznej / manifest is not canonical JSON")
        if manifest.get("schema") != SCHEMA or manifest.get("algorithm") != "Ed25519":
            errors.append(f"nieznany schemat / unknown schema: {manifest.get('schema')!r}")
        out.update(run_id=manifest.get("run_id"), audit_chain_head=manifest.get("audit_chain_head"),
                   product=manifest.get("product"), created_at=manifest.get("created_at"))
        try:
            pub_raw = base64.b64decode(str(manifest.get("public_key", "")), validate=True)
            pub = Ed25519PublicKey.from_public_bytes(pub_raw)
            sig = base64.b64decode(src.read(SIG_NAME).strip(), validate=True)
        except ValueError as exc:
            errors.append(f"klucz publiczny lub podpis uszkodzony / public key or signature malformed: {exc}")
            return out
        fp = fingerprint(pub_raw)
        out["key_fingerprint"] = fp
        if manifest.get("key_fingerprint") != fp:
            errors.append("odcisk klucza w manifeście nie pasuje do klucza / key fingerprint field mismatch")
        if expected_fingerprint and expected_fingerprint.strip() != fp:
            errors.append(f"inny klucz niż oczekiwany / key differs from expected: {fp} != {expected_fingerprint}")
        try:
            pub.verify(sig, body)
        except InvalidSignature:
            errors.append("podpis Ed25519 nieprawidłowy / Ed25519 signature invalid")
        listed: dict[str, dict[str, Any]] = {}
        declared = 0
        for f in manifest.get("files") or []:
            rel = f.get("path") if isinstance(f, dict) else None
            if not isinstance(rel, str) or not safe_member(rel) or rel in SIGNATURE_FILES:
                errors.append(f"niebezpieczna ścieżka w manifeście / unsafe path in manifest: {rel!r}")
                continue
            if rel in listed:
                errors.append(f"powtórzona ścieżka / duplicate path: {rel}")
            listed[rel] = f
            declared += int(f.get("size") or 0)
        if declared > MAX_VERIFY_BYTES:
            errors.append(f"deklarowany rozmiar za duży / declared size too large: {declared}")
            return out
        present = names - SIGNATURE_FILES
        for rel in sorted(set(listed) - present):
            errors.append(f"brak pliku / missing file: {rel}")
        for rel in sorted(present - set(listed)):
            errors.append(f"dodatkowy plik / extra file: {rel}")
        for rel in sorted(set(listed) & present):
            digest, size = src.digest(rel, int(listed[rel].get("size") or 0))
            out["files_checked"] += 1
            if digest != listed[rel].get("sha256"):
                errors.append(f"hash nie pasuje / sha256 mismatch: {rel}")
            elif size != listed[rel].get("size"):
                errors.append(f"rozmiar nie pasuje / size mismatch: {rel}")
        out["files_listed"] = len(listed)
        out["ok"] = not errors
        return out
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, TypeError) as exc:
        errors.append(f"błąd odczytu / read error: {exc}")
        return out
    finally:
        src.close()
