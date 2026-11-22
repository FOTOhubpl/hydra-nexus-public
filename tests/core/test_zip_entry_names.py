"""AUDIT B1 / B11: ZIP entry names without the UTF-8 flag (bit 11) and encrypted entries.

Info-ZIP `zip` (macOS/Linux) writes raw UTF-8 name bytes without the flag; Windows Explorer (Polish locale) writes
cp852. Python decodes both as cp437 (`zdj─Öcie 40.png`). Synthetic ZIPs only: the name bytes are patched after
writing because `zipfile` always sets the flag for non-ASCII names.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from hydra.adapters.nask_folder import unpack_zip, zip_skipped
from hydra.ingest import Skipped, _safe_extract, zip_entry_name
from tests.conftest import png_bytes

NAME = "zdjęcie 40.png"


def _zip_without_utf8_flag(out: Path, entries: dict[str, bytes], encoding: str, *, encrypt: bool = False) -> Path:
    """A ZIP whose entry names are raw `encoding` bytes with general purpose bit 11 cleared."""
    placeholders = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for i, (name, data) in enumerate(entries.items()):
            raw = name.encode(encoding)
            ph = f"{i:02d}".encode() + b"X" * (len(raw) - 2)
            placeholders[ph] = raw
            z.writestr(ph.decode("ascii"), data)
    blob = bytearray(out.read_bytes())
    for sig, flag_off, len_off, name_off in ((b"PK\x03\x04", 6, 26, 30), (b"PK\x01\x02", 8, 28, 46)):
        i = blob.find(sig)
        while i != -1:
            flags = int.from_bytes(blob[i + flag_off:i + flag_off + 2], "little") & ~0x800
            if encrypt:
                flags |= 0x1
            blob[i + flag_off:i + flag_off + 2] = flags.to_bytes(2, "little")
            n = int.from_bytes(blob[i + len_off:i + len_off + 2], "little")  # file name length field
            ph = bytes(blob[i + name_off:i + name_off + n])
            blob[i + name_off:i + name_off + n] = placeholders[ph]
            i = blob.find(sig, i + 4)
    out.write_bytes(bytes(blob))
    with zipfile.ZipFile(out) as z:  # the fixture really has the flag unset
        assert all(not (info.flag_bits & 0x800) for info in z.infolist())
    return out


@pytest.mark.parametrize("encoding", ["utf-8", "cp852"])
def test_zip_entry_name_recovers_polish_name(tmp_path, encoding):
    z = _zip_without_utf8_flag(tmp_path / "a.zip", {NAME: b"x", "REKLAMA DUŻA.PNG": b"y"}, encoding)
    with zipfile.ZipFile(z) as zf:
        assert zf.infolist()[0].filename != NAME  # what Python alone gives (cp437 mojibake)
        assert [zip_entry_name(i) for i in zf.infolist()] == [NAME, "REKLAMA DUŻA.PNG"]


def test_zip_entry_name_keeps_cp437_when_not_polish(tmp_path):
    z = _zip_without_utf8_flag(tmp_path / "a.zip", {"MENÜ.TXT": b"x"}, "cp437")
    with zipfile.ZipFile(z) as zf:
        assert zip_entry_name(zf.infolist()[0]) == "MENÜ.TXT"


def test_flagged_utf8_name_is_unchanged(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(NAME, b"x")
    with zipfile.ZipFile(z) as zf:
        assert zip_entry_name(zf.infolist()[0]) == NAME


@pytest.mark.parametrize("encoding", ["utf-8", "cp852"])
def test_ingest_zip_keeps_polish_names_and_traversal_guard(tmp_path, settings, encoding):
    z = _zip_without_utf8_flag(tmp_path / "a.zip", {NAME: png_bytes(), "../zdjęcie poza.png": png_bytes(),
                                                    "..\\złośliwy.png": png_bytes()}, encoding)
    dest = tmp_path / "out"
    dest.mkdir()
    out = _safe_extract(z, dest, settings)
    refs = {(o.source_ref if not isinstance(o, tuple) else o[0]): o for o in out}
    assert not isinstance(refs[f"a.zip!/{NAME}"], tuple)
    bad = [o for o in out if isinstance(o, tuple)]
    assert {o[1] for o in bad} == {"ZIP_PATH_TRAVERSAL"} and len(bad) == 2
    assert {o[0] for o in bad} == {"a.zip!/../zdjęcie poza.png", "a.zip!/..\\złośliwy.png"}
    assert not list(tmp_path.glob("zdj*"))


@pytest.mark.parametrize("encoding", ["utf-8", "cp852"])
def test_nask_unpack_zip_writes_polish_names_and_traversal_guard(tmp_path, settings, encoding):
    z = _zip_without_utf8_flag(tmp_path / "a.zip", {f"incydent_1/{NAME}": png_bytes(),
                                                    "../zdjęcie poza.png": png_bytes()}, encoding)
    root = unpack_zip(z, settings)
    assert (root / NAME).is_file() or (root / "incydent_1" / NAME).is_file()
    assert {(s["path"], s["code"]) for s in zip_skipped(z, settings)} == {
        ("a.zip!/../zdjęcie poza.png", "ZIP_PATH_TRAVERSAL")}
    assert not any(p.name == "zdjęcie poza.png" for p in settings.tmp_dir.parent.rglob("*.png"))


def test_encrypted_entry_has_explicit_code(tmp_path, settings):
    z = _zip_without_utf8_flag(tmp_path / "e.zip", {"tajne.png": png_bytes(), NAME: png_bytes()}, "utf-8",
                               encrypt=True)
    dest = tmp_path / "out"
    dest.mkdir()
    out: list = _safe_extract(z, dest, settings)
    assert all(isinstance(o, tuple) for o in out)
    skipped: list[Skipped] = out
    assert {(s[0], s[1]) for s in skipped} == {("e.zip!/tajne.png", "ZIP_ENTRY_ENCRYPTED"),
                                              (f"e.zip!/{NAME}", "ZIP_ENTRY_ENCRYPTED")}
    assert all("zaszyfrowany" in s[2] for s in skipped)
    unpack_zip(z, settings)
    codes = {s["code"] for s in zip_skipped(z, settings)}
    assert codes == {"ZIP_ENTRY_ENCRYPTED"}


def test_encrypted_code_has_polish_status():
    from hydra.jury_report import _skip_code_pl

    assert "zaszyfrowany" in _skip_code_pl("ZIP_ENTRY_ENCRYPTED")
