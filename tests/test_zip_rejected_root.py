import zipfile
from pathlib import Path

from hydra.adapters import nask_folder


def test_rejected_root_zip_does_not_abort_dataset(tmp_path: Path, monkeypatch):
    root = tmp_path / "ds"
    (root / "a1").mkdir(parents=True)
    (root / "a1" / "page.html").write_text("<html><body>ok</body></html>")
    bad = root / "bomb.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("x/page.html", "<html></html>")

    def boom(path, settings=None):
        raise nask_folder.ZipRejected("ZIP_BOMB_SUSPECTED")

    monkeypatch.setattr(nask_folder, "unpack_zip", boom)
    nask_folder.REJECTED_ROOT_ARCHIVES.clear()
    found = nask_folder.discover_incidents(root)
    assert [p.name for p in found] == ["a1"]
    assert nask_folder.REJECTED_ROOT_ARCHIVES == [{"path": "bomb.zip", "code": "ZIP_BOMB_SUSPECTED"}]
