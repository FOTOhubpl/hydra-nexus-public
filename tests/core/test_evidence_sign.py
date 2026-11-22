"""Signed evidence package (spec 3.0 acceptance test 7): Ed25519 over a canonical manifest of every file."""

from __future__ import annotations

import base64
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from hydra.config import Settings
from hydra.db import Writer
from hydra.events import canonical_json
from hydra.evidence_sign import (
    MANIFEST_NAME,
    SIG_NAME,
    key_info,
    key_path,
    load_or_create_key,
    public_raw,
    safe_member,
    sign_package,
    verify_package,
)


@pytest.fixture
def pkg(tmp_path: Path, settings: Settings) -> Path:
    d = tmp_path / "pkg"
    (d / "sub").mkdir(parents=True)
    (d / "incidents.csv").write_text("incident_id\nnask-1\n")
    (d / "campaigns.json").write_text(json.dumps([{"id": "C-1", "members": ["nask-1"]}]))
    (d / "sub" / "genes.parquet").write_bytes(bytes(range(256)) * 4)
    sign_package(settings, d, "run-1")
    return d


def _zip(folder: Path, out: Path, root: str = "hydra-dataset-run-1", extra: dict[str, bytes] | None = None,
         drop: str | None = None, mutate: str | None = None) -> Path:
    with zipfile.ZipFile(out, "w") as zf:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                rel = p.relative_to(folder).as_posix()
                if rel == drop:
                    continue
                data = p.read_bytes()
                if rel == mutate:
                    data = bytes([data[0] ^ 1]) + data[1:]
                zf.writestr(f"{root}/{rel}" if root else rel, data)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return out


def test_key_created_once_with_0600(settings: Settings) -> None:
    k1 = load_or_create_key(settings)
    p = key_path(settings)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert public_raw(load_or_create_key(settings).public_key()) == public_raw(k1.public_key())
    assert key_info(settings)["fingerprint"].startswith("SHA256:")


def test_valid_folder_and_zip_pass(pkg: Path, settings: Settings, tmp_path: Path) -> None:
    man = json.loads((pkg / MANIFEST_NAME).read_text())
    assert (pkg / MANIFEST_NAME).read_bytes() == canonical_json(man).encode()
    assert {f["path"] for f in man["files"]} == {"incidents.csv", "campaigns.json", "sub/genes.parquet"}
    assert man["run_id"] == "run-1" and man["audit_chain_head"] is None  # no DB yet in this fixture
    assert man["key_fingerprint"] == key_info(settings)["fingerprint"]
    for target in (pkg, _zip(pkg, tmp_path / "ok.zip"), _zip(pkg, tmp_path / "flat.zip", root="")):
        res = verify_package(target, key_info(settings)["fingerprint"])
        assert res["ok"], res["errors"]
        assert res["files_checked"] == 3


def test_audit_chain_head_recorded(tmp_path: Path, settings: Settings) -> None:
    from hydra.db import connect

    w = Writer(settings.db_path)
    w.close()
    with connect(settings.db_path) as conn:
        from hydra import events

        h = events.append(conn, "TEST", {"x": 1})
    d = tmp_path / "p2"
    d.mkdir()
    (d / "a.txt").write_text("a")
    man = sign_package(settings, d, None)
    assert man["audit_chain_head"]["hash"] == h and man["audit_chain_head"]["seq"] >= 1


def test_tampered_byte_fails_and_names_file(pkg: Path, tmp_path: Path) -> None:
    res = verify_package(_zip(pkg, tmp_path / "t.zip", mutate="campaigns.json"))
    assert not res["ok"] and any("sha256 mismatch: campaigns.json" in e for e in res["errors"])
    f = pkg / "sub" / "genes.parquet"
    b = bytearray(f.read_bytes())
    b[100] ^= 0xFF
    f.write_bytes(bytes(b))
    res = verify_package(pkg)
    assert not res["ok"] and res["errors"] == ["hash nie pasuje / sha256 mismatch: sub/genes.parquet"]


def test_removed_file_fails(pkg: Path, tmp_path: Path) -> None:
    res = verify_package(_zip(pkg, tmp_path / "r.zip", drop="incidents.csv"))
    assert not res["ok"] and any("missing file: incidents.csv" in e for e in res["errors"])
    res = verify_package(_zip(pkg, tmp_path / "r2.zip", drop=SIG_NAME))
    assert not res["ok"]


def test_added_file_fails(pkg: Path, tmp_path: Path) -> None:
    res = verify_package(_zip(pkg, tmp_path / "a.zip", extra={"hydra-dataset-run-1/extra.txt": b"x"}))
    assert not res["ok"] and any("extra file: extra.txt" in e for e in res["errors"])
    (pkg / "new.csv").write_text("x")
    assert not verify_package(pkg)["ok"]


def test_manifest_edit_breaks_signature(pkg: Path) -> None:
    man = json.loads((pkg / MANIFEST_NAME).read_text())
    man["run_id"] = "run-2"
    (pkg / MANIFEST_NAME).write_bytes(canonical_json(man).encode())
    res = verify_package(pkg)
    assert not res["ok"] and any("signature invalid" in e for e in res["errors"])


def test_swapped_signature_fails(pkg: Path, tmp_path: Path, settings: Settings) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("other package, same key")
    sign_package(settings, other, "run-9")
    (pkg / SIG_NAME).write_bytes((other / SIG_NAME).read_bytes())
    res = verify_package(pkg)
    assert not res["ok"] and any("signature invalid" in e for e in res["errors"])


def test_wrong_key_fails(pkg: Path, settings: Settings) -> None:
    """Signature by another key: fails against the embedded key; a re-signed package fails a pinned fingerprint."""
    attacker = Ed25519PrivateKey.generate()
    body = (pkg / MANIFEST_NAME).read_bytes()
    (pkg / SIG_NAME).write_bytes(base64.b64encode(attacker.sign(body)))
    assert any("signature invalid" in e for e in verify_package(pkg)["errors"])
    # attacker re-signs with their own key and embeds it: self-consistent, but not the expected installation key
    man = json.loads(body)
    from hydra.evidence_sign import fingerprint

    man["public_key"] = base64.b64encode(public_raw(attacker.public_key())).decode()
    man["key_fingerprint"] = fingerprint(attacker.public_key())
    body2 = canonical_json(man).encode()
    (pkg / MANIFEST_NAME).write_bytes(body2)
    (pkg / SIG_NAME).write_bytes(base64.b64encode(attacker.sign(body2)))
    assert verify_package(pkg)["ok"]  # integrity only; identity needs the fingerprint
    res = verify_package(pkg, key_info(settings)["fingerprint"])
    assert not res["ok"] and any("key differs from expected" in e for e in res["errors"])


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/passwd", "a/../../b", "C:\\x", "a\\..\\b", "a//b", "./a",
                                  "a\nb", ""])
def test_safe_member_rejects_traversal(name: str) -> None:
    assert not safe_member(name)


def test_traversal_in_zip_and_manifest_rejected(pkg: Path, tmp_path: Path) -> None:
    res = verify_package(_zip(pkg, tmp_path / "tz.zip", extra={"../evil.txt": b"x"}))
    assert not res["ok"] and any("unsafe ZIP member name" in e for e in res["errors"])
    man = json.loads((pkg / MANIFEST_NAME).read_text())
    man["files"].append({"path": "../../etc/passwd", "sha256": "0" * 64, "size": 1})
    (pkg / MANIFEST_NAME).write_bytes(canonical_json(man).encode())
    res = verify_package(pkg)
    assert not res["ok"] and any("unsafe path in manifest" in e for e in res["errors"])


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_symlink_in_folder_rejected(pkg: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.txt").write_text("secret")
    (pkg / "link.txt").symlink_to(tmp_path / "outside.txt")
    res = verify_package(pkg)
    assert not res["ok"] and any("symlink" in e for e in res["errors"])


def test_not_a_package(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"nope")
    assert not verify_package(f)["ok"]
    d = tmp_path / "empty"
    d.mkdir()
    assert any("missing" in e for e in verify_package(d)["errors"])


def test_cli_verify_export_exit_codes(pkg: Path, tmp_path: Path, settings: Settings, monkeypatch) -> None:
    from hydra.cli import app

    monkeypatch.setenv("HYDRA_STATE_DIR", str(settings.state_dir))
    monkeypatch.setenv("HYDRA_ROOT", str(tmp_path))
    r = CliRunner().invoke(app, ["verify-export", str(pkg), "--local-key"])
    assert r.exit_code == 0 and r.output.startswith("PASS"), r.output
    bad = _zip(pkg, tmp_path / "bad.zip", mutate="incidents.csv")
    r = CliRunner().invoke(app, ["verify-export", str(bad)])
    assert r.exit_code == 1 and r.output.startswith("FAIL") and "incidents.csv" in r.output
