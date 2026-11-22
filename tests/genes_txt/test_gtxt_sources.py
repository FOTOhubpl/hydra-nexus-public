"""HTML / MHTML / text-file / OCR-zone parsing on synthetic inputs."""

from __future__ import annotations

from email.message import EmailMessage

from hydra.genome.ocr_pool import OcrBox
from hydra.genome.txt_sources import html_blocks, read_html, text_file_blocks, zone_lines

HTML = """<!DOCTYPE html><html lang="en"><head><title>Szokujące: prezes aresztowany — Fałszywy Portal</title>
<meta name="description" content="Opis strony z obietnicą zysku.">
<script>var x = "nie pokazuj";</script><style>.a{}</style></head>
<body>
<nav class="main-nav"><a>Wiadomości</a> <a>Sport</a> <a>Moto</a></nav>
<article>
 <h1>Prezes aresztowany po wywiadzie na żywo</h1>
 <p class="article-lead">Wywiad usunięty z anteny wieczorem, ale zdążyliśmy je zapisać dla czytelników portalu.</p>
 <div class="author-box">oprac. Anna Testowa</div>
 <p>Platforma inwestycyjna Nova Kapitał wymaga minimalnej wpłaty 950 zł. Doradca zadzwoni w ciągu godziny.</p>
 <p style="display:none">ukryty tekst</p>
 <div id="goog-gt-tt">Oryginalny tekst</div>
</article>
<form action="/order"><input name="phone" placeholder="511 222 333"><button>Zarejestruj się</button></form>
<div class="comments-section">
 <div class="comment"><span class="comment-author">Jan Kowalski</span> Zarobiłem 3 000 zł w tydzień!</div>
 <div class="comment"><span class="comment-author">Ewa Nowak</span> Polecam każdemu.</div>
</div>
</body></html>"""


def _sources(blocks):
    return {b.source: b for b in blocks}


def test_html_blocks_structure():
    blocks = _sources(html_blocks(HTML, "a1"))
    assert blocks["html_title"].text.startswith("Szokujące")
    assert blocks["html_headline"].text == "Prezes aresztowany po wywiadzie na żywo"
    assert blocks["html_lead"].text.startswith("Wywiad usunięty")
    body = blocks["html_body"].text
    assert "Nova Kapitał" in body and "ukryty" not in body and "nie pokazuj" not in body
    assert "Oryginalny" not in body and "Wiadomości" not in body and "Zarobiłem" not in body
    assert "Wiadomości" in blocks["html_nav"].text
    assert len(blocks["html_comment"].segments) == 2
    assert {s.text for s in blocks["html_commenter"].segments} == {"Jan Kowalski", "Ewa Nowak"}
    assert "511 222 333" in blocks["html_form"].text
    assert "Anna Testowa" in blocks["html_byline"].text
    assert blocks["html_body"].meta["declared_lang"] == "en"
    seg = blocks["html_headline"].segments[0]
    assert seg.evidence.artifact_id == "a1" and seg.evidence.dom_path.endswith("h1")


def test_mhtml_is_decoded(tmp_path):
    msg = EmailMessage()
    msg["MIME-Version"] = "1.0"
    msg["Subject"] = "saved page"
    msg.set_content(HTML, subtype="html", cte="quoted-printable")
    p = tmp_path / "page.mhtml"
    p.write_bytes(msg.as_bytes())
    assert "Prezes aresztowany" in read_html(p)


def test_text_file_blocks(tmp_path):
    p = tmp_path / "chat.txt"
    p.write_text("Cześć\n\nZainwestuj 1000 zł na platformie\n", "utf-8")
    (b,) = text_file_blocks(p, "t1")
    assert [s.text for s in b.segments] == ["Cześć", "Zainwestuj 1000 zł na platformie"]
    assert b.segments[1].evidence.char_range == (7, 39)


def _box(t, y, x=40, h=40):
    return OcrBox(t, (x, y, x + 600, y + h), 0.99)


def test_phone_feed_zones_separate_ad_from_feed_noise():
    lines = [_box("innyuser.pl", 200), _box("Post sąsiada o wakacjach", 260), _box("Sweet Casino", 400),
             _box("Sponsorowane", 445), _box("WYGRAJ 4 000 000 ZŁ", 1400), _box("Zagraj w grę", 1700),
             _box("marcin.nowak", 2200), _box("Obserwuj", 2200, x=800), _box("Zupełnie inny post", 2280)]
    z = {b.text: zone for b, zone, _ in zone_lines(lines, 1170, 2532)}
    assert z["Post sąsiada o wakacjach"] == "feed" and z["Zupełnie inny post"] == "feed"
    assert z["Sweet Casino"] == "ad" and z["WYGRAJ 4 000 000 ZŁ"] == "ad"
    # desktop crop: no feed segmentation
    assert {zone for _, zone, _ in zone_lines(lines, 600, 700)} == {"ad"}


def test_library_panel_zones():
    lines = [_box("Identyfikator biblioteki: 1234567890123", 700), _box("Sweet Casino", 1500),
             _box("Sponsorowane", 1550), _box("Bath rugs extra soft", 1620), _box("Zamknij", 2000)]
    z = {b.text: zone for b, zone, _ in zone_lines(lines, 1170, 2532)}
    assert z["Bath rugs extra soft"] == "library_ad" and z["Sweet Casino"] == "library_ad"
    assert z["Identyfikator biblioteki: 1234567890123"] == "library_panel"
