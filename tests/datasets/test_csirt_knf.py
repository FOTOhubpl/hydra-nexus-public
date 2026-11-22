"""csirt-knf adapter on a synthetic review page (no real data)."""

from __future__ import annotations

import json
from pathlib import Path

from hydra.adapters.datasets import csirt_knf

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 4000
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 4000
SLUG = "przeglad-wybranych-oszustw-internetowych-lipiec-2026"
PAGE = """<html><body><div class="sidebar"><img src="/images/logo.png"></div>
<div class="article" itemprop="articleBody">
<h2>Przegląd wybranych oszustw internetowych - Lipiec 2026</h2>
<p><img src="/images/R/banner.png" /></p>
<p>Wstęp do przeglądu.</p>
<ul><li>oferty fałszywych inwestycji,</li></ul>
<p><strong>OFERTY FAŁSZYWYCH INWESTYCJI</strong></p>
<p>Oszuści publikowali reklamy fałszywych inwestycji na Facebooku (rys. 1).</p>
<p><img src="/images/R/1.png" alt="1" /></p>
<p style="text-align: center;">Rysunek 1 Fałszywa reklama na portalu Facebook</p>
<p><strong>PODSZYCIA BEZPOŚREDNIO POD BANKI</strong></p>
<p>Przestępcy regularnie podszywają się pod polskie banki.</p>
<p><strong>BEZPIECZNA WERYFIKACJA TOŻSAMOŚCI</strong></p>
<p>Cyberprzestępcy przygotowali fałszywe strony, na których podszywali się pod PKO BP.</p>
<p><img src="/images/R/2%20a.png" /></p>
<p>Rysunek 2 Fałszywa strona podszywająca się pod PKO BP &ndash; dane 1/2</p>
<p><img src="/images/R/3.jpg" /></p>
<p>Rysunek 3 Fałszywa wiadomość SMS &ndash; podszycie pod PKO BP 2/2</p>
<p><strong>Schemat ataku:</strong></p>
<p><img src="/images/R/missing.png" /></p>
<p>Rysunek 4 Obraz, którego nie pobrano</p>
<div class="inner"><p>Akapit w zagnieżdżonym divie.</p></div>
<p><strong>KARTA PALIWOWA</strong>Oszuści oferowali karty paliwowe Orlen za 9 zł.</p>
</div>
<div class="footer"><p><strong>STOPKA SERWISU</strong></p><p>Treść stopki.</p></div>
</body></html>"""


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "csirt-knf"
    (root / "pages").mkdir(parents=True)
    img = root / "images" / SLUG
    img.mkdir(parents=True)
    (root / "pages" / f"{SLUG}.html").write_text(PAGE, encoding="utf-8")
    files = {"/images/R/banner.png": ("banner.png", PNG), "/images/R/1.png": ("1.png", PNG),
             "/images/R/2%20a.png": ("2_a.png", PNG), "/images/R/3.jpg": ("3.jpg", JPG)}
    images = []
    for src, (name, data) in files.items():
        (img / name).write_bytes(data)
        images.append({"src": src, "path": f"images/{SLUG}/{name}"})
    images.append({"src": "/images/R/missing.png", "error": "HTTP Error 404"})
    (root / "manifest.json").write_text(json.dumps({"reviews": [
        {"slug": SLUG, "month": "lipiec", "year": 2026, "url": "https://example.invalid/" + SLUG, "images": images},
        {"slug": "not-fetched", "month": "maj", "year": 2026, "images": []},
    ]}), encoding="utf-8")
    return root


def test_csirt_knf_index_and_load(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    rows = csirt_knf.index(root)
    keys = [r.key for r in rows]
    assert keys == ["2026-07/oferty-falszywych-inwestycji/00", "2026-07/bezpieczna-weryfikacja-tozsamosci/00",
                    "2026-07/bezpieczna-weryfikacja-tozsamosci/01", "2026-07/karta-paliwowa/text"]
    by = {r.key: r for r in rows}
    ver = [r for r in rows if r.label == "2026-07/bezpieczna-weryfikacja-tozsamosci"]
    assert len(ver) == 2 and all(r.group == "bank" for r in ver)   # one named case -> one label, two figures
    assert by["2026-07/oferty-falszywych-inwestycji/00"].group == "investment"
    assert by["2026-07/karta-paliwowa/text"].extra["image"] is None  # heading + text in one <p>
    assert all(r.extra["lang"] == "pl" for r in rows)
    assert not any("STOPKA" in r.extra["heading"] or "stopki" in r.extra["desc"] for r in rows)  # body ends

    incs = {i.key.split("-", 1)[1]: i for i in csirt_knf.load(root, rows)}
    ad = incs["2026-07/oferty-falszywych-inwestycji/00"]
    assert set(ad.files) == {"ad_text.txt", "ad_creative.png"} and ad.roles == {}
    assert ad.meta["figure_kind"] == "ad" and ad.meta["lang"] == "pl"
    assert ad.source == "ext:csirt-knf"
    assert ad.label == "ext:csirt-knf:2026-07/oferty-falszywych-inwestycji"
    assert ad.group_label == "ext:csirt-knf:investment"
    page = incs["2026-07/bezpieczna-weryfikacja-tozsamosci/00"]
    assert page.roles == {"screenshot.png": "landing_screenshot"}
    text = page.files["ad_text.txt"].decode()
    assert text.startswith("BEZPIECZNA WERYFIKACJA TOŻSAMOŚCI\n\nCyberprzestępcy przygotowali")
    assert "Rysunek 2 Fałszywa strona podszywająca się pod PKO BP – dane 1/2" in text
    # mixed-case <strong> is not a case heading; a nested <div> does not end the article body
    assert "Schemat ataku:" in text and "Akapit w zagnieżdżonym divie." in text
    assert "Rysunek 4" not in text and "Orlen" not in text  # missing image dropped; next case not mixed in
    assert page.meta["impersonated"] == "pko bp"
    sms = incs["2026-07/bezpieczna-weryfikacja-tozsamosci/01"]
    assert sms.roles == {"screenshot.jpg": "feed_screenshot"} and sms.meta["figure_kind"] == "message"
    fuel = incs["2026-07/karta-paliwowa/text"]
    assert set(fuel.files) == {"ad_text.txt"} and "Orlen" in fuel.files["ad_text.txt"].decode()
    assert all("url.txt" not in i.files for i in incs.values())


def test_csirt_knf_where_and_empty(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    rows = csirt_knf.index(root)
    assert csirt_knf.where() is None
    pick = [r for r in rows if csirt_knf.where(with_image=True, scheme="bank")(r)]
    assert len(pick) == 2
    assert csirt_knf.index(tmp_path / "nothing") == []
    assert csirt_knf.article_body("<p>no body</p>") == ""
