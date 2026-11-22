"""Synthetic inputs of the G4 scenarios (generated at test time from tests/e2e/corpus.py, never organiser data).

    python -m tests.e2e.g4_fixtures OUT_DIR BASE_DIR   -> prints a JSON map of the generated paths

- known/  : a new campaign-A incident: a CROPPED ad screenshot of base/a2 (new bytes, same creative) and its
            landing-page variant /oferta/106 (scenario 1);
- kit/    : a landing page of an unrelated lure (other host, phone, IBAN, brand, scheme) built with the same page
            kit as campaigns A/B (same HTML skeleton and lead form) (scenario 2);
- foreign/: an English-language page and photo with no shared indicator (scenario 3);
- corrupt.png (PNG magic + garbage), bad.zip (truncated ZIP) for the invalid-input steps.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import zipfile
from pathlib import Path

from PIL import Image

from tests.e2e import corpus


def build(out: Path, base: Path) -> dict[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    src = next((base / "a2").glob("Zrzut ekranu*.png"))
    im = Image.open(src).convert("RGB")
    w, h = im.size
    crop = im.crop((int(w * 0.04), int(h * 0.06), int(w * 0.80), int(h * 0.78)))  # ~55 % of the area
    known = out / "known"
    corpus._campaign_a(known, 6)  # new landing-page variant /oferta/106 of campaign A ...
    for f in known.glob("Zrzut ekranu*.png"):
        f.unlink()  # ... whose ad creative is only the cropped screenshot below
    (known / "page.har").unlink()
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    (known / "kadr-reklamy-pko.png").write_bytes(buf.getvalue())

    kit = out / "kit"
    kit.mkdir(exist_ok=True)
    url = "https://lotto-wygrana.click/odbierz/77?utm_source=fb"
    body = ("Gratulacje! Wygrałeś iPhone 17 w loterii Lotto Premium. Opłać wysyłkę 9,99 zł kartą."
            " Infolinia +48 668 551 207.")
    (kit / "Strona.html").write_text(corpus.html_page(url, "Lotto Premium wygrana", body), encoding="utf-8")

    foreign = out / "foreign"
    foreign.mkdir(exist_ok=True)
    furl = "https://birdwatching-club.example.org/notes/heron"
    (foreign / "page.html").write_text(
        f"<!-- saved from url=({len(furl):04d}){furl} -->\n<html lang=\"en\"><head><title>Grey heron notes</title>"
        "</head><body><article><h2>Grey heron notes</h2><p>Seen at dawn near the old mill pond,"
        " standing still for twenty minutes before catching a small roach.</p></article></body></html>",
        encoding="utf-8")
    (foreign / "heron.png").write_bytes(corpus.photo("Grey heron", 97))

    (out / "corrupt.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00garbage-not-an-image" * 64)
    good = io.BytesIO()
    with zipfile.ZipFile(good, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("incydent/Strona.html", corpus.html_page(url, "x", body) * 20)
    (out / "bad.zip").write_bytes(good.getvalue()[: len(good.getvalue()) // 2])

    # pending{1,2}.zip: the base corpus with a trailing byte sequence per file (new sha256, no dedup/cache hit), so a
    # corpus job on it runs long enough (~20 s) to be in flight when the restart check stops the server
    for n in (1, 2):
        pend = out / "pending" / "base"
        shutil.rmtree(out / "pending", ignore_errors=True)
        shutil.copytree(base, pend)
        for f in sorted(p for p in pend.rglob("*") if p.is_file()):
            tail = f"\n<!-- g4-pending-{n} -->\n" if f.suffix in (".html", ".txt", ".har") else f"\0g4-{n}"
            f.write_bytes(f.read_bytes() + tail.encode())
        corpus.zip_folder(pend, out / f"pending{n}.zip")
        shutil.rmtree(out / "pending", ignore_errors=True)
    return {"known": str(known), "kit": str(kit), "foreign": str(foreign), "corrupt_png": str(out / "corrupt.png"),
            "bad_zip": str(out / "bad.zip"), "pending_zip": str(out / "pending1.zip"),
            "pending2_zip": str(out / "pending2.zip")}


if __name__ == "__main__":
    print(json.dumps(build(Path(sys.argv[1]), Path(sys.argv[2]))))
