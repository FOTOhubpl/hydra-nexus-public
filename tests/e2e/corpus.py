"""Synthetic end-to-end corpus, generated at test time (never committed, never organiser data).

Layout follows the NASK sample: one folder per incident. Base corpus:

- campaign A "pko-bonus" (investment lure): saved landing page (HTML with `saved from url`, Meta pixel, phone,
  IBAN), ad screenshot (PNG with rendered Polish text), HAR of the landing page;
- campaign B "tani-buty" (fake shop): landing page, a PDF "regulamin" with a text layer, a WhatsApp-style chat
  export with the shop phone; one incident delivers its page inside a ZIP;
- campaign C "paczka-doplata" (parcel surcharge): SMS screenshot, a short screen recording (MP4), chat export;
- negatives: unrelated benign pages (gardening, recipes...), each with its own host, phone and text.

New batch (assign-batch): three items of an unseen campaign D "energa-zwrot", one byte-identical duplicate of a
base incident (A1) and one fresh item of campaign A.
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

A = {"host": "pko-bonus24.com", "phone": "+48 512 734 981", "iban": "PL61109010140000071219812874",
     "pixel": "987654321012345", "title": "Inwestycja PKO Bonus",
     "text": "Inwestuj z PKO i zarabiaj 5000 zł dziennie. Zadzwoń do doradcy {phone}. Przelew na {iban}."}
B = {"host": "tani-buty24.pl", "phone": "+48 600 200 300", "iban": "PL27114020040000300201355387",
     "title": "Buty wyprzedaż -90%",
     "text": "Wyprzedaż markowych butów -90%. Zamów teraz, płatność BLIK lub przelew {iban}. Kontakt {phone}."}
C = {"host": "inpost-doplata.top", "phone": "+48 733 112 909",
     "text": "InPost: Twoja paczka czeka. Dopłata 1,99 zł wymagana: https://inpost-doplata.top/pl/{n}"}
D = {"host": "energa-zwrot.online", "phone": "+48 791 404 222", "iban": "PL83101010230000261395100000",
     "title": "Energa zwrot nadpłaty",
     "text": "Energa: przysługuje Ci zwrot nadpłaty 482,17 zł. Potwierdź dane karty. Infolinia {phone}, {iban}."}

CAMPAIGN_A = ["a1", "a2", "a3", "a4"]
CAMPAIGN_B = ["b1", "b2", "b3", "b4"]
CAMPAIGN_C = ["c1", "c2", "c3", "c4"]
NEGATIVES = [f"n{i}" for i in range(1, 9)]
NEW_D = ["new-d1", "new-d2", "new-d3"]
NEW_DUP = "new-dup-a1"
NEW_A = "new-a5"


def _font(size: int):  # noqa: ANN202
    for p in FONTS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def png(text: str, color: tuple[int, int, int], size: tuple[int, int] = (720, 480), seed: int = 0) -> bytes:
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    d.rectangle((30 + seed % 7, 40, size[0] - 30, 120), fill=(255, 255, 255))
    d.text((50, 60), text[:40], fill=(10, 10, 10), font=_font(30))
    y = 160
    for i in range(0, min(len(text), 200), 45):
        d.text((50, y), text[i:i + 45], fill=(250, 250, 250), font=_font(24))
        y += 40
    d.ellipse((size[0] - 140, size[1] - 140, size[0] - 40, size[1] - 40), fill=(255, 200, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def html_page(url: str, title: str, body: str, pixel: str | None = None) -> str:
    px = f"<script>fbq('init','{pixel}');fbq('track','PageView');</script>" if pixel else ""
    return (f"<!-- saved from url=({len(url):04d}){url} -->\n<!DOCTYPE html><html lang=\"pl\"><head>"
            f"<meta charset=\"utf-8\"><title>{title}</title>{px}</head><body><h1>{title}</h1><p>{body}</p>"
            f"<form action=\"/api/lead\" method=\"post\"><input name=\"phone\"><input name=\"name\">"
            f"<button>Wyślij</button></form></body></html>")


def har(url: str, html: str) -> str:
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "e2e", "version": "1"},
                               "pages": [{"id": "page_1", "title": url, "startedDateTime": "2026-10-01T10:00:00Z"}],
                               "entries": [{"pageref": "page_1", "startedDateTime": "2026-10-01T10:00:00Z",
                                            "request": {"method": "GET", "url": url, "headers": []},
                                            "response": {"status": 200, "headers": [],
                                                         "content": {"mimeType": "text/html", "text": html}},
                                            "serverIPAddress": "203.0.113.7"}]}})


def pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with a real text layer (Helvetica, WinAnsi; Polish letters transliterated)."""
    tr = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
    ops = ["BT", "/F1 12 Tf", "50 780 Td"]
    for ln in lines:
        safe = ln.translate(tr).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops += [f"({safe}) Tj", "0 -18 Td"]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >>"
            b" /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def mp4(path: Path, text: str, color: tuple[int, int, int], frames: int = 24) -> None:
    """A 2-second 12 fps screen recording: the SMS screenshot scrolling slightly (OpenCV mp4v)."""
    import cv2
    import numpy as np

    base = np.array(Image.open(io.BytesIO(png(text, color, (480, 320)))).convert("RGB"))[:, :, ::-1]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (480, 320))
    for i in range(frames):
        vw.write(np.roll(base, i * 2, axis=0))
    vw.release()


def chat(lines: list[tuple[str, str]], day: int) -> str:
    return "\n".join(f"[{day:02d}.10.2026, 1{i % 10}:0{i % 6}] {who}: {msg}" for i, (who, msg) in enumerate(lines))


def _campaign_a(d: Path, n: int) -> None:
    url = f"https://{A['host']}/oferta/{100 + n}?utm_source=fb&utm_campaign=bonus{n}"
    body = A["text"].format(**A)
    page = html_page(url, A["title"], body, A["pixel"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "Strona.html").write_text(page, encoding="utf-8")
    (d / f"Zrzut ekranu 2026-10-0{n} o 10.1{n}.png").write_bytes(
        png(f"PKO Bonus: zarabiaj 5000 zł dziennie! {A['phone']}", (20, 60, 150), seed=n))
    (d / "page.har").write_text(har(url, page), encoding="utf-8")


def _campaign_b(d: Path, n: int, zipped: bool = False) -> None:
    url = f"https://{B['host']}/sklep/promocja-{n}"
    page = html_page(url, B["title"], B["text"].format(**B))
    d.mkdir(parents=True, exist_ok=True)
    if zipped:
        with zipfile.ZipFile(d / "strona.zip", "w") as z:
            z.writestr("Strona.html", page)
    else:
        (d / "Strona.html").write_text(page, encoding="utf-8")
    (d / "regulamin.pdf").write_bytes(pdf([f"Regulamin sklepu {B['host']}", f"Kontakt: {B['phone']}",
                                           f"Wplaty na rachunek {B['iban']}", "Zwroty nie sa przyjmowane."]))
    (d / "czat.txt").write_text(chat([("Sklep", f"Dzień dobry, buty Nike -90%, pisz na {B['phone']}"),
                                      ("Ja", "Ile kosztuje dostawa?"),
                                      ("Sklep", f"Darmowa, przelew na {B['iban']} i wysyłamy")], n), encoding="utf-8")


def _campaign_c(d: Path, n: int) -> None:
    text = C["text"].format(n=4000 + n)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"IMG_10{n}2.PNG").write_bytes(png(text, (240, 190, 20), seed=n))
    mp4(d / f"ScreenRecording_10-0{n}-2026 12-0{n}-11_1.MP4", text, (240, 190, 20))
    (d / "czat.txt").write_text(chat([(C["phone"], text), ("Ja", "Co to za dopłata?"),
                                      (C["phone"], "Prosimy zapłacić w ciągu 24h")], n), encoding="utf-8")


def _campaign_d(d: Path, n: int) -> None:
    url = f"https://{D['host']}/zwrot/{n}?ref=sms"
    page = html_page(url, D["title"], D["text"].format(**D))
    d.mkdir(parents=True, exist_ok=True)
    (d / "Strona.html").write_text(page, encoding="utf-8")
    (d / f"Zrzut ekranu 2026-11-0{n}.png").write_bytes(
        png(f"Energa zwrot 482,17 zł {D['phone']}", (0, 140, 90), seed=n))
    (d / "czat.txt").write_text(chat([(D["phone"], D["text"].format(**D))], n), encoding="utf-8")


NEG = [  # (host, path, title, body): unrelated benign pages, no shared host shape, template, phone or form
    ("ogrodnik-amator.pl", "/porady/kompost", "Kompostownik krok po kroku",
     "Jak założyć kompostownik w ogrodzie działkowym i czego do niego nie wrzucać."),
    ("kuchnia.babci-zosi.com.pl", "/przepisy/pierogi-ruskie", "Pierogi ruskie",
     "Ciasto z mąki, wody i odrobiny oleju; farsz z ziemniaków, twarogu i smażonej cebuli."),
    ("remontujemy.net", "/lazienka/fugi.html", "Fugowanie płytek",
     "Fugę epoksydową nakładamy gumową pacą, a nadmiar zmywamy wilgotną gąbką."),
    ("wedkarz.org.pl", "/forum/temat/88231", "Leszcze nad Zalewem Zegrzyńskim",
     "Wczoraj od świtu brały leszcze na białe robaki, łowisko przy główce mostu."),
    ("serwis-rowerowy-krakow.eu", "/blog/regulacja-przerzutki", "Regulacja tylnej przerzutki",
     "Najpierw ustaw śrubę baryłkową, potem ograniczniki H i L, na końcu napięcie linki."),
    ("storczyki.info", "/phalaenopsis/podlewanie", "Podlewanie falenopsisa",
     "Zanurz doniczkę w letniej wodzie na kwadrans raz w tygodniu, zimą rzadziej."),
    ("tatromaniak.blog", "/2026/09/orla-perc", "Orla Perć we wrześniu",
     "Szlak jednokierunkowy z Zawratu na Kozi Wierch, zalecany kask i wczesny start."),
    ("gitara-od-zera.edu.pl", "/lekcja-3/akordy", "Trzy pierwsze akordy",
     "Akordy G, C i D wystarczą do zagrania kilkuset piosenek przy ognisku."),
]


def _negative(d: Path, i: int) -> None:
    host, path, title, body = NEG[i - 1]
    url = f"https://{host}{path}"
    d.mkdir(parents=True, exist_ok=True)
    page = (f"<!-- saved from url=({len(url):04d}){url} -->\n<html><head><title>{title}</title></head><body>"
            f"<article><h2>{title}</h2>" + "".join(f"<p>{w}</p>" for w in body.split(", "))
            + "</article></body></html>")
    (d / "Strona.html").write_text(page, encoding="utf-8")
    (d / f"Zrzut ekranu {title}.png").write_bytes(photo(title, i))


def photo(title: str, seed: int, size: tuple[int, int] = (720, 480)) -> bytes:
    """A distinct 'photo' per negative: random shapes on a random background, no shared layout with the lures."""
    import random

    rnd = random.Random(seed * 7919)
    im = Image.new("RGB", size, tuple(rnd.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x0, y0 = rnd.randrange(size[0]), rnd.randrange(size[1])
        box = (x0, y0, x0 + rnd.randrange(20, 300), y0 + rnd.randrange(20, 200))
        fill = tuple(rnd.randrange(256) for _ in range(3))
        (d.ellipse if rnd.random() < 0.5 else d.rectangle)(box, fill=fill)
    d.text((rnd.randrange(20, 300), rnd.randrange(20, 400)), title, fill=(0, 0, 0), font=_font(28))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def build_base(root: Path) -> Path:
    """The base dataset folder (one subfolder per incident)."""
    base = root / "base"
    for n, name in enumerate(CAMPAIGN_A, 1):
        _campaign_a(base / name, n)
    for n, name in enumerate(CAMPAIGN_B, 1):
        _campaign_b(base / name, n, zipped=(n == 4))
    for n, name in enumerate(CAMPAIGN_C, 1):
        _campaign_c(base / name, n)
    for i, name in enumerate(NEGATIVES, 1):
        _negative(base / name, i)
    return base


def build_new(root: Path, base: Path) -> Path:
    new = root / "new"
    for n, name in enumerate(NEW_D, 1):
        _campaign_d(new / name, n)
    shutil.copytree(base / CAMPAIGN_A[0], new / NEW_DUP)
    _campaign_a(new / NEW_A, 5)
    return new


def zip_folder(folder: Path, out: Path) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(folder.parent).as_posix())
    return out


def build(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    base = build_base(root)
    new = build_new(root, base)
    return {"base": base, "new": new, "base_zip": zip_folder(base, root / "base.zip")}
