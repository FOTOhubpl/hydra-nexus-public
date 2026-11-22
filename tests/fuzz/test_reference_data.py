"""Offline reference-data bundle (scripts/reference_data.py): build from a synthetic external/ tree, install with
checksum verification, refuse unsafe members; reference_status reports what is present."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import reference_data  # noqa: E402


def _fake_external(root: Path) -> Path:
    ext = root / "external"
    snap = ext / "misp-warninglists" / "abc123"
    (snap / "lists" / "x").mkdir(parents=True)
    (snap / "lists" / "x" / "list.json").write_text(json.dumps({"name": "x", "list": ["example.com"]}))
    (snap / "README.md").write_text("CC0")
    (ext / "misp-warninglists" / "current").symlink_to("abc123")
    (ext / "psl").mkdir(parents=True)
    (ext / "psl" / "public_suffix_list.dat").write_text("// MPL\ncom\npl\n")
    (ext / "cert").mkdir(parents=True)
    (ext / "cert" / "domains.json").write_text("[]")
    (ext / "cert" / "SNAPSHOT.json").write_text(json.dumps({"url": "https://hole.cert.pl/domains/v2/domains.json"}))
    (ext / "knf").mkdir()
    (ext / "knf" / "secret.json").write_text("{}")
    return root


def test_build_install_roundtrip_excludes_knf(tmp_path, monkeypatch):
    bundle = reference_data.build(_fake_external(tmp_path / "src"), tmp_path / "out")
    dest = tmp_path / "data" / "external"
    res = reference_data.install(bundle, dest)
    assert all(v.startswith("ok") for v in res.values()), res
    assert (dest / "misp-warninglists" / "abc123" / "lists" / "x" / "list.json").is_file()
    assert (dest / "psl" / "SOURCE.md").is_file() and (dest / "cert" / "SOURCE.md").is_file()
    assert not (dest / "knf").exists()
    # status from the installed tree
    from hydra.config import Settings
    from hydra.jury_report import reference_status

    monkeypatch.setenv("HYDRA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HYDRA_MISP_WL_DIR", raising=False)
    monkeypatch.delenv("HYDRA_CERT_PL_DIR", raising=False)
    st = reference_status(Settings(root=tmp_path, data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                                   cache_dir=tmp_path / "cache", models_dir=tmp_path / "models"))
    assert st["misp_warninglists"] and st["psl"] and st["cert_pl"]
    assert not any("danych referencyjnych offline" in n for n in st["notes_pl"])
    # a tampered file is reported
    (dest / "psl" / "public_suffix_list.dat").write_text("tampered")
    assert "MISMATCH" in reference_data.verify(dest)["psl"]


def test_missing_sets_give_a_polish_note(tmp_path, monkeypatch):
    from hydra.config import Settings
    from hydra.jury_report import reference_status

    monkeypatch.setenv("HYDRA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HYDRA_MISP_WL_DIR", raising=False)
    monkeypatch.delenv("HYDRA_CERT_PL_DIR", raising=False)
    st = reference_status(Settings(root=tmp_path, data_dir=tmp_path / "data", state_dir=tmp_path / "state",
                                   cache_dir=tmp_path / "cache", models_dir=tmp_path / "models"))
    assert not st["face_gallery"] and not st["misp_warninglists"]
    assert any("Brak danych referencyjnych offline" in n for n in st["notes_pl"])
    assert any("galeria twarzy" in n for n in st["notes_pl"])


@pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "psl/../../evil.txt", "other/x.txt"])
def test_install_refuses_unsafe_members(tmp_path, name):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    bad = tmp_path / "bad.tar"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(SystemExit):
        reference_data.install(bad, tmp_path / "dest")
    assert not (tmp_path / "evil.txt").exists()


def test_install_refuses_links(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("psl/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    bad = tmp_path / "bad.tar"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(SystemExit):
        reference_data.install(bad, tmp_path / "dest")
