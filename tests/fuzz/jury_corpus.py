"""Synthetic jury-import corpora (JURY-READY). Generated at test time; never organiser data, never committed.

Two builders:

- `build_fuzz(root)`: one folder per *layout* the jury may hand us, each a separate input for `hydra analyse`
  or a GUI upload. Every hostile or odd file must end with a visible per-file status and must never stop the
  analysis of the valid incidents next to it.
- `build_scale(root, n)`: a NASK-like dataset of `n` incident folders (campaigns of 2..8 incidents sharing a
  host, phone, IBAN and pixel, plus singletons), for wall-time / RSS measurement.

CLI (writes outside the repo; the path must not be under the real state):

    uv run python -m tests.fuzz.jury_corpus fuzz  /tmp/jury-fuzz
    uv run python -m tests.fuzz.jury_corpus scale /tmp/jury-scale-200 --n 200 [--video-every 10]
    uv run python -m tests.fuzz.jury_corpus graphdemo /tmp/jury-graphdemo   # Neo4j proof corpus (docs/NEO4J.md)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import struct
import sys
import zipfile
from pathlib import Path

from tests.e2e.corpus import har, html_page, mp4, photo, png

# ---------------------------------------------------------------------------------------------------- pieces

LURES = [
    ("Inwestycja z bankiem", "Zarabiaj {k} zł dziennie z naszym doradcą. Zadzwoń {phone}. Wpłata na {iban}."),
    ("Dopłata do paczki", "Twoja paczka czeka na dopłatę {k},99 zł. Kliknij link i zapłać kartą: {url}"),
    ("Zwrot nadpłaty za prąd", "Przysługuje Ci zwrot {k},17 zł. Potwierdź dane karty. Infolinia {phone}."),
    ("Wyprzedaż butów -90%", "Markowe buty taniej o {k}%. Płatność BLIK lub przelew {iban}. Kontakt {phone}."),
    ("Kryptowaluty dla seniorów", "Gwarantowany zysk {k}% w miesiąc. Opiekun {phone}, konto {iban}."),
]
TLDS = ("pl", "com", "online", "top", "shop", "info")


def _iban(rnd: random.Random) -> str:
    return "PL" + "".join(str(rnd.randrange(10)) for _ in range(26))


def _phone(rnd: random.Random) -> str:
    return f"+48 {rnd.randrange(500, 900)} {rnd.randrange(100, 1000)} {rnd.randrange(100, 1000)}"


def campaign_facts(cid: int, rnd: random.Random) -> dict:
    title, text = LURES[cid % len(LURES)]
    host = f"{['oferta', 'zwrot', 'paczka', 'promo', 'bonus'][cid % 5]}-{cid:04d}-{rnd.randrange(10**6):06d}." \
           f"{TLDS[cid % len(TLDS)]}"
    return {"cid": cid, "title": f"{title} {cid}", "text": text, "host": host, "phone": _phone(rnd),
            "iban": _iban(rnd), "pixel": str(rnd.randrange(10**14, 10**15)),
            "color": (rnd.randrange(30, 220), rnd.randrange(30, 220), rnd.randrange(30, 220))}


def write_incident(d: Path, f: dict, n: int, *, video: bool = False, har_file: bool = True) -> None:
    """One NASK-like incident: ad screenshot, saved landing page, HAR, short description; optional recording."""
    d.mkdir(parents=True, exist_ok=True)
    url = f"https://{f['host']}/lp/{n}?utm_source=fb&utm_campaign=c{f['cid']}"
    body = f["text"].format(k=100 + n, phone=f["phone"], iban=f["iban"], url=url)
    page = html_page(url, f["title"], body, f["pixel"])
    (d / "strona.html").write_text(page, encoding="utf-8")
    (d / f"Zrzut ekranu {n}.png").write_bytes(png(f"{f['title']} {body}", f["color"], seed=n))
    if har_file:
        (d / "ruch.har").write_text(har(url, page), encoding="utf-8")
    (d / "opis.txt").write_text(f"Zgłoszenie {n}: reklama na Facebooku prowadzi do {url}\n{body}\n", encoding="utf-8")
    if video:
        mp4(d / "nagranie.mp4", body, f["color"])


def write_singleton(d: Path, i: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    host = f"ogrod-{i:04d}-przepisy.pl"
    url = f"https://{host}/artykul/{i}"
    words = ["pomidory", "rozsada", "kompost", "konewka", "szklarnia", "dynia", "bazylia", "jabłoń"]
    rnd = random.Random(i * 31)
    body = " ".join(rnd.choice(words) for _ in range(40))
    (d / "strona.html").write_text(html_page(url, f"Poradnik {i}", body), encoding="utf-8")
    (d / f"zdjęcie {i}.png").write_bytes(photo(f"Poradnik {i}", 1000 + i))


def corrupt_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + os.urandom(64)


def corrupt_jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + os.urandom(200)


def corrupt_mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + os.urandom(300)


def broken_html() -> bytes:
    return b"<html><head><title>Niedoko\xc5\x84czona<body><div><div><p>" + b"<span>" * 5000 + b"\xff\xfe garbage"


def mhtml(url: str, html: str, png_bytes: bytes, qp: bool = False) -> bytes:
    import base64
    import quopri

    b = "----=_NextPart_jury"
    if qp:
        part = quopri.encodestring(html.encode("utf-8")).decode("ascii")
        enc = "quoted-printable"
    else:
        part = base64.encodebytes(html.encode("utf-8")).decode("ascii")
        enc = "base64"
    img = base64.encodebytes(png_bytes).decode("ascii")
    return (f"From: <Saved by Blink>\r\nSnapshot-Content-Location: {url}\r\nSubject: strona\r\n"
            f"MIME-Version: 1.0\r\nContent-Type: multipart/related; type=\"text/html\"; boundary=\"{b}\"\r\n\r\n"
            f"--{b}\r\nContent-Type: text/html; charset=\"utf-8\"\r\nContent-Transfer-Encoding: {enc}\r\n"
            f"Content-Location: {url}\r\n\r\n{part}\r\n"
            f"--{b}\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
            f"Content-Location: {url}/logo.png\r\n\r\n{img}\r\n--{b}--\r\n").encode("ascii")


def har_variants(url: str, html: str) -> dict[str, str]:
    import base64

    ok = json.loads(har(url, html))
    b64 = json.loads(har(url, html))
    c = b64["log"]["entries"][0]["response"]["content"]
    c["text"], c["encoding"] = base64.b64encode(html.encode()).decode(), "base64"
    no_pages = json.loads(har(url, html))
    no_pages["log"].pop("pages")
    return {"ruch.har": json.dumps(ok), "ruch-base64.har": json.dumps(b64), "bez-stron.har": json.dumps(no_pages),
            "uszkodzony.har": json.dumps(ok)[: len(json.dumps(ok)) // 2]}


def encrypted_zip(out: Path, inner: dict[str, bytes]) -> Path:
    """A ZIP whose entries carry the 'encrypted' flag (ZipCrypto header bit 0): every reader refuses them."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        for name, data in inner.items():
            z.writestr(name, data)
    raw = bytearray(out.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        i = raw.find(sig)
        while i != -1:
            flags = struct.unpack_from("<H", raw, i + off)[0]
            struct.pack_into("<H", raw, i + off, flags | 0x1)
            i = raw.find(sig, i + 4)
    out.write_bytes(bytes(raw))
    return out


def zip_bomb(out: Path, mb: int = 64) -> Path:
    """Highly compressible entry (ratio far above HYDRA_MAX_ZIP_RATIO): must be rejected before unpacking."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z, z.open("zera.txt", "w") as fh:
        chunk = b"\x00" * (1024 * 1024)
        for _ in range(mb):
            fh.write(chunk)
    return out


def traversal_zip(out: Path, good_png: bytes) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("../../poza-archiwum.txt", "ucieczka")
        z.writestr("/etc/absolutna.txt", "absolutna")
        z.writestr("incydent/reklama.png", good_png)
        info = zipfile.ZipInfo("incydent/link")
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    return out


def zip_dir(folder: Path, out: Path, *, arc_root: Path | None = None) -> Path:
    base = arc_root if arc_root is not None else folder.parent
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and not p.is_symlink():
                z.write(p, p.relative_to(base).as_posix())
    return out


# ---------------------------------------------------------------------------------------------- fuzz layouts

LONG = "bardzo-długa-nazwa-pliku-zrzutu-ekranu-z-reklamy-" * 6  # trimmed to 240 bytes by _long_name


def _long_name(stem: str, ext: str, limit: int = 240) -> str:
    name = stem
    while len((name + ext).encode("utf-8")) > limit:
        name = name[:-1]
    return name + ext


def _weird_incident(d: Path, f: dict, rnd: random.Random) -> list[str]:
    """One valid incident surrounded by every odd file type. Returns the names expected to be rejected/skipped."""
    write_incident(d, f, 1)
    good = png(f"{f['title']} wariant", f["color"], seed=7)
    (d / "REKLAMA DUŻA.PNG").write_bytes(good)                                   # uppercase extension
    (d / "to-jest-png.htm").write_bytes(good)                                     # wrong extension (PNG bytes)
    (d / "strona-jako.jpg").write_text(html_page(f"https://{f['host']}/x", f["title"], "treść"), encoding="utf-8")
    (d / "pusty.txt").write_bytes(b"")                                            # zero bytes
    (d / "uszkodzony.png").write_bytes(corrupt_png())
    (d / "uszkodzony.jpg").write_bytes(corrupt_jpeg())
    (d / "uszkodzony.mp4").write_bytes(corrupt_mp4())
    (d / "zepsuta.html").write_bytes(broken_html())
    (d / "duży tekst.txt").write_text(("Zażółć gęślą jaźń. " * 60 + "\n") * 2500, encoding="utf-8")  # ~3 MB
    (d / "wiadomość-cp1250.txt").write_bytes(f"Pilne: dopłata do przesyłki, zadzwoń {f['phone']} – Łódź, Gdańsk"
                                             .encode("cp1250"))
    (d / "wiadomość-iso8859-2.txt").write_bytes("Świętokrzyskie: zwrot podatku, źródło żądań".encode("iso-8859-2"))
    url = f"https://{f['host']}/lp/mhtml"
    page = html_page(url, f["title"], f["text"].format(k=7, phone=f["phone"], iban=f["iban"], url=url))
    (d / "strona.mhtml").write_bytes(mhtml(url, page, good))
    (d / "strona-qp.mht").write_bytes(mhtml(url, page, good, qp=True))
    for name, text in har_variants(url, page).items():
        (d / ("v-" + name)).write_text(text, encoding="utf-8")
    (d / _long_name(LONG, ".png")).write_bytes(png("długa nazwa", f["color"], seed=11))
    (d / "binarka.bin").write_bytes(os.urandom(2048))                             # unknown MIME
    (d / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1" + os.urandom(64))      # OS junk
    (d / "Thumbs.db").write_bytes(os.urandom(512))
    encrypted_zip(d / "zahasłowane.zip", {"tajne.png": good, "tajne.txt": b"haslo"})
    zip_bomb(d / "bomba.zip")
    traversal_zip(d / "traversal.zip", good)
    try:
        (d / "dowiązanie-do-passwd").symlink_to("/etc/passwd")
        (d / "dowiązanie-do-zrzutu.png").symlink_to(d / "REKLAMA DUŻA.PNG")
        (d / "pętla").symlink_to(d / "pętla")
    except OSError:
        pass
    return ["pusty.txt", "zahasłowane.zip", "bomba.zip", "traversal.zip", "dowiązanie-do-passwd"]


def build_fuzz(root: Path, seed: int = 2026) -> dict[str, Path]:
    """Layouts (each value is one analyse input):

    nask_folders   dataset: 4 incidents in 2 campaigns + 1 byte-identical copy of an incident
    wrapper        one wrapper folder holding the dataset (`wrapper/Dane testowe/p1..p3`)
    nested         dataset nested two levels deep (`kategoria/podkategoria/incydent`): imported per category
                   folder, with a note in the import report
    top_zip        one ZIP holding a wrapper folder with incident folders
    zip_in_zip     ZIP of incident ZIPs, one of them holding another ZIP
    loose_root     files directly at the input root (no incident folders)
    mixed          incident folders + incident ZIPs + loose README + the 'weird' incident + hostile root ZIPs
    one_incident   a single incident folder (no pairs possible)
    singletons     3 unrelated incidents
    identical      3 byte-identical incidents
    nothing        dataset whose incidents hold only zero-byte / junk files (zero analysable files)
    weird          the hostile incident alone
    """
    rnd = random.Random(seed)
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    c1, c2, c3 = (campaign_facts(i, rnd) for i in (1, 2, 3))
    out: dict[str, Path] = {}

    d = root / "nask_folders"
    for n in (1, 2):
        write_incident(d / f"incydent_{n:03d}", c1, n)
    for n in (3, 4):
        write_incident(d / f"incydent_{n:03d}", c2, n, video=(n == 4))
    shutil.copytree(d / "incydent_001", d / "incydent_005 kopia")
    out["nask_folders"] = d

    d = root / "wrapper"
    for n in (1, 2):
        write_incident(d / "Dane testowe" / f"p{n}", c3, 70 + n)
    write_singleton(d / "Dane testowe" / "p3", 73)
    out["wrapper"] = d

    d = root / "nested"
    write_incident(d / "kategoria A" / "podkategoria 1" / "Zgłoszenie Łódź 1", c3, 1)
    write_incident(d / "kategoria A" / "podkategoria 1" / "Zgłoszenie Łódź 2", c3, 2)
    write_singleton(d / "kategoria B" / "inne" / "Zgłoszenie 3", 3)
    out["nested"] = d

    staging = root / "_staging"
    w = staging / "Zbiór jury"
    for n in (1, 2):
        write_incident(w / f"inc-{n}", c1, 10 + n)
    write_singleton(w / "inc-3", 4)
    out["top_zip"] = zip_dir(w, root / "zbiór-jury.zip")

    zz = staging / "zz"
    zz.mkdir(parents=True)
    for n in (1, 2):
        write_incident(staging / f"z{n}" / f"incydent-{n}", c2, 20 + n)
        zip_dir(staging / f"z{n}" / f"incydent-{n}", zz / f"incydent-{n}.zip")
    write_incident(staging / "z3" / "inner", c2, 23)
    zip_dir(staging / "z3" / "inner", staging / "z3" / "inner.zip")
    (staging / "z3" / "incydent-3").mkdir()
    shutil.move(str(staging / "z3" / "inner.zip"), staging / "z3" / "incydent-3" / "wewnętrzny.zip")
    (staging / "z3" / "incydent-3" / "opis.txt").write_text("ZIP w ZIP-ie", encoding="utf-8")
    zip_dir(staging / "z3" / "incydent-3", zz / "incydent-3.zip")
    out["zip_in_zip"] = zip_dir(zz, root / "zip-w-zipie.zip", arc_root=zz)

    d = root / "loose_root"
    d.mkdir()
    write_incident(d, c3, 30)
    (d / "README.md").write_text("Luźne pliki w katalogu głównym", encoding="utf-8")
    out["loose_root"] = d

    d = root / "mixed"
    write_incident(d / "Zażółć gęślą jaźń — incydent 7", c1, 7)
    write_incident(staging / "m" / "incydent-zip", c1, 8)
    zip_dir(staging / "m" / "incydent-zip", d / "incydent-zip.zip")
    write_singleton(d / "inny", 9)
    (d / "README.txt").write_text("Opis zbioru jury", encoding="utf-8")
    (d / "pusty folder").mkdir()
    zip_bomb(d / "bomba-w-korzeniu.zip")
    encrypted_zip(d / "hasło-w-korzeniu.zip", {"a/reklama.png": png("x", (1, 2, 3))})
    (d / "zepsuty.zip").write_bytes(b"PK\x03\x04" + os.urandom(300))
    _weird_incident(d / "dziwne pliki", c2, rnd)
    shutil.copytree(d / "Zażółć gęślą jaźń — incydent 7", d / "duplikat incydentu 7")
    out["mixed"] = d

    d = root / "one_incident" / "jedyny incydent"
    write_incident(d, c1, 40)
    out["one_incident"] = d

    d = root / "singletons"
    for i in (1, 2, 3):
        write_singleton(d / f"s{i}", 50 + i)
    out["singletons"] = d

    d = root / "identical"
    write_incident(d / "i1", c3, 60)
    for k in ("i2", "i3"):
        shutil.copytree(d / "i1", d / k)
    out["identical"] = d

    d = root / "nothing"
    for k in ("a", "b"):
        (d / k).mkdir(parents=True)
        (d / k / "pusty.png").write_bytes(b"")
        (d / k / ".DS_Store").write_bytes(b"junk")
    out["nothing"] = d

    d = root / "weird" / "dziwne pliki"
    _weird_incident(d, c2, rnd)
    out["weird"] = d

    shutil.rmtree(staging)
    return out


# ------------------------------------------------------------------------------------------------- scale


def build_scale(root: Path, n: int, seed: int = 7, video_every: int = 0, singleton_share: float = 0.3) -> Path:
    """`n` incident folders: ~70 % in campaigns of 2..8 incidents, the rest singletons."""
    rnd = random.Random(seed)
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    i = cid = 0
    n_camp = round(n * (1 - singleton_share))
    while i < n_camp:
        f = campaign_facts(cid, rnd)
        size = min(rnd.randrange(2, 9), n_camp - i) or 1
        for k in range(size):
            i += 1
            write_incident(root / f"incydent_{i:05d}", f, k + 1,
                           video=bool(video_every) and i % video_every == 0, har_file=(k % 2 == 0))
        cid += 1
    while i < n:
        i += 1
        write_singleton(root / f"incydent_{i:05d}", i)
    return root


def build_graph_demo(root: Path, seed: int = 11) -> Path:
    """Small corpus for the Neo4j proof (docs/NEO4J.md): three campaigns (5, 4, 3 incidents), the operator phone of
    campaign 0 reused by campaign 1, one incident that carries the IBAN of campaign 0 and the landing host of
    campaign 2 (a bridge), and four unrelated singletons. Every value is synthetic."""
    rnd = random.Random(seed)
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    facts = [campaign_facts(c, rnd) for c in (0, 3, 4)]  # lures whose text carries both a phone and an IBAN
    facts[1]["phone"] = facts[0]["phone"]  # operator phone reused across campaigns 0 and 1
    i = 0
    for f, size in zip(facts, (5, 4, 3), strict=True):
        for k in range(size):
            i += 1
            write_incident(root / f"incydent_{i:05d}", f, k + 1, har_file=(k % 2 == 0))
    i += 1
    write_incident(root / f"incydent_{i:05d}", facts[2] | {"iban": facts[0]["iban"]}, 9)  # bridge 0 <-> 2
    for _ in range(4):
        i += 1
        write_singleton(root / f"incydent_{i:05d}", i)
    return root


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("fuzz", "scale", "graphdemo"))
    ap.add_argument("out", type=Path)
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--video-every", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    out = a.out.expanduser().resolve()
    state = Path.home() / "HydraNexus" / "state"
    if out == state or state in out.parents:
        print("refusing to write under the real state dir", file=sys.stderr)
        return 2
    if a.what == "fuzz":
        print(json.dumps({k: str(v) for k, v in build_fuzz(out).items()}, indent=2, ensure_ascii=False))
    elif a.what == "graphdemo":
        print(build_graph_demo(out))
    else:
        print(build_scale(out, a.n, seed=a.seed, video_every=a.video_every))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


