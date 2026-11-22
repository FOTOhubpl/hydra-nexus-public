"""Harder hidden-test-set simulator (ASSIGN-QUALITY, 2026-11-21). SYNTHETIC material, generated on the server only.

NASK §6: the hidden set holds new variants of known campaigns and material from other campaigns; it checks
finding variants, assignment to existing groups, open-set rejection and robustness to duplicates, incomplete
data and format differences. This script derives, from every NASK sample incident, the variant types such a set
is likely to contain, plus hard negatives, and scores a product run (`hydra assign-batch`) against the truth.

Positives (expect = the source incident's campaign in the base run):
  dup_exact, zip               exact copy / ZIP of the folder
  img5_any                     5 random images (>= 20 kB) from anywhere in the folder (incl. saved-page assets)
  img3_top, creative1          up to 3 / the largest top-level image (the ad creatives / screenshots)
  creative_crop                top-level images cropped 6-12 % on random sides, resized 0.7-0.9, JPEG q60-80
  creative_recolor             hue/saturation/brightness shift, slight resize, JPEG
  creative_overlay             a 12-18 % banner with new text over the top or bottom, JPEG
  paraphrase                   page text: numbers changed, names swapped, sentences reordered; domains rotated;
                               HAR dropped (a new variant on a new domain without a network log)
  newdom_kit                   first-party domains rotated everywhere (HAR kept), trackers/kit/paths unchanged
  landing_shot                 only a rendered screenshot of the saved landing page (offline headless Chromium,
                               all DNS mapped to NOTFOUND, so nothing leaves the machine)
  html_no_har                  everything but the HAR
  html_only                    only the saved page (HTML + _files): no creatives, no HAR, no metadata
  no_meta                      JSON/TXT metadata dropped, top-level files renamed to generic names
  video_reenc                  only the video, trimmed 1 s and re-encoded at 0.6x size
Negatives (expect = new):
  ext_purino, ext_phresh       external scam-ad / phishing incidents (corpus-demo)
  ext_brand                    external incidents mentioning the sample's persons/brands (same brand, other operator)
  tpl_clone                    a sample page's tag skeleton with every text node, asset, tracker and domain replaced
  brand_creative               a synthetic news-style ad naming the sample's persons/brands over an unrelated photo
  generic_lead                 a generic lead-form landing (jQuery, Google Fonts, an FB pixel loader with another id,
                               name/phone form, Cloudflare-like IP) -- the QA-3 "unrelated upload with kit bits" case
  url_only                     a text file with one public-feed lure URL (offline: URL/domain genes only)

Usage (server):
  python -m eval.hidden_sim make --out ~/HydraNexus/assignq/sim-s1 --seed 1
  hydra analyse <sample>; hydra assign-batch <out>/new --out <exports>
  python -m eval.hidden_sim score --truth <out>/truth.csv --assignments <exports>/assignments.json
  python -m eval.hidden_sim rescore --truth ... --assignments ... [--json out.json]   # re-decide with current code
Prints aggregates only (no organiser content).
"""

from __future__ import annotations

import argparse
import colorsys
import csv
import io
import json
import random
import re
import shutil
import subprocess
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from eval.variants import FICTIONAL_NAMES, WORDS, m_drop_har, m_new_domain, template_clone

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VID_EXT = {".mp4", ".mov", ".m4v", ".webm"}
HOME = Path.home() / "HydraNexus"
POSITIVE_TYPES = ["dup_exact", "zip", "img5_any", "img3_top", "creative1", "creative_crop", "creative_recolor",
                  "creative_overlay", "paraphrase", "newdom_kit", "landing_shot", "html_no_har", "html_only",
                  "no_meta", "video_reenc"]
NEGATIVE_TYPES = ["ext_purino", "ext_phresh", "ext_brand", "tpl_clone", "brand_creative", "generic_lead", "url_only"]
# Persons and media brands for the brand-overlap negatives. Sample-specific terms stay outside git in the local
# lexicon overlay (`$HYDRA_DATA_DIR/nask/lexicons/hidden_sim_brands.json`: {"persons": [...], "brands": [...],
# "grep": "<regex>"}); without it the generator uses fictional placeholders.
def _brand_overlay() -> tuple[list[str], list[str], str]:
    from hydra.genome.lexicons import local
    d = local.load("hidden_sim_brands") or {}
    persons = d.get("persons") or ["Jan Przykładowy", "Anna Fikcyjna", "Piotr Testowy"]
    brands = d.get("brands") or ["Telewizja Przykład", "Portal Example", "Bank Testowy"]
    return persons, brands, d.get("grep") or "|".join(re.escape(t) for t in persons + brands) + "|inwestyc"


BRAND_PERSONS, BRAND_MEDIA, _BRAND_RX = _brand_overlay()
BRAND_TERMS = BRAND_PERSONS + BRAND_MEDIA
BRAND_GREP = re.compile(_BRAND_RX, re.I)
CHROME_GLOB = ".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
SYN_PL = {"platforma": "aplikacja", "zarobić": "zyskać", "pieniądze": "środki", "teraz": "dziś",
          "szybko": "błyskawicznie", "inwestycja": "lokata", "Polacy": "Rodacy", "codziennie": "każdego dnia",
          "bardzo": "naprawdę", "dzisiaj": "dziś", "zarejestruj": "załóż konto",
          "Zarejestruj": "Załóż konto", "ludzie": "osoby"}


# --------------------------------------------------------------------------------------- helpers


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _top_images(root: Path) -> list[Path]:
    return sorted((p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXT),
                  key=lambda p: -p.stat().st_size)


def _is_har(p: Path) -> bool:
    return p.suffix.lower() == ".har" or p.name == ".har"


def _main_html(root: Path) -> Path | None:
    pages = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".html", ".htm"}
             and not any(part.endswith("_files") for part in p.relative_to(root).parts[:-1])]
    return max(pages, key=lambda p: p.stat().st_size) if pages else None


def _save_jpeg(im, path: Path, q: int) -> Path:
    out = path.with_suffix(".jpg")
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=q)
    out.write_bytes(buf.getvalue())
    if out != path:
        path.unlink(missing_ok=True)
    return out


def _edit_top_images(root: Path, rng: random.Random, fn) -> int:
    from PIL import Image

    n = 0
    for p in _top_images(root):
        try:
            with Image.open(p) as im:
                im.load()
                im = im.convert("RGB")
        except Exception:  # noqa: BLE001 -- unreadable image: leave it
            continue
        if im.width < 48 or im.height < 48:
            continue
        _save_jpeg(fn(im), p, rng.randint(60, 80))
        n += 1
    return n


def _keep_only(root: Path, keep: list[Path]) -> None:
    keep_set = {k.resolve() for k in keep}
    for p in _files(root):
        if p.resolve() not in keep_set:
            p.unlink()
    for d in sorted((d for d in root.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        if not any(d.iterdir()):
            d.rmdir()


# --------------------------------------------------------------------------------------- positive variants


def v_img5_any(root: Path, rng: random.Random) -> str | None:
    imgs = [p for p in _files(root) if p.suffix.lower() in IMG_EXT and p.stat().st_size > 20_000]
    if len(imgs) < 2:
        return None
    pick = rng.sample(imgs, min(5, len(imgs)))
    flat = []
    for k, p in enumerate(pick):
        dst = root / f"obraz_{k + 1}{p.suffix.lower()}"
        shutil.copy2(p, dst.with_name(dst.name + ".tmp"))
        flat.append(dst)
    for p in _files(root):
        if not p.name.endswith(".tmp"):
            p.unlink()
    for d in sorted((d for d in root.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        shutil.rmtree(d, ignore_errors=True)
    for dst in flat:
        dst.with_name(dst.name + ".tmp").rename(dst)
    return f"img5_any:{len(pick)}"


def v_img3_top(root: Path, rng: random.Random) -> str | None:
    imgs = _top_images(root)[:3]
    if not imgs:
        return None
    _keep_only(root, imgs)
    return f"img3_top:{len(imgs)}"


def v_creative1(root: Path, rng: random.Random) -> str | None:
    imgs = _top_images(root)[:1]
    if not imgs:
        return None
    _keep_only(root, imgs)
    return "creative1"


def _creative_only(root: Path) -> bool:
    imgs = _top_images(root)
    if not imgs:
        return False
    _keep_only(root, imgs)
    return True


def v_creative_crop(root: Path, rng: random.Random) -> str | None:
    if not _creative_only(root):
        return None

    def fn(im):
        w, h = im.size
        box = [int(w * rng.uniform(0, 0.12)), int(h * rng.uniform(0, 0.12)),
               w - int(w * rng.uniform(0, 0.12)), h - int(h * rng.uniform(0, 0.12))]
        im = im.crop(box)
        s = rng.uniform(0.7, 0.9)
        return im.resize((max(32, int(im.width * s)), max(32, int(im.height * s))))

    return f"creative_crop:{_edit_top_images(root, rng, fn)}"


def v_creative_recolor(root: Path, rng: random.Random) -> str | None:
    if not _creative_only(root):
        return None
    from PIL import Image, ImageEnhance

    def fn(im):
        shift = rng.uniform(0.04, 0.12)
        hsv = im.convert("HSV")
        h, s, v = hsv.split()
        h = h.point(lambda x: int((x + shift * 255) % 256))
        im = Image.merge("HSV", (h, s, v)).convert("RGB")
        im = ImageEnhance.Color(im).enhance(rng.uniform(0.7, 1.3))
        im = ImageEnhance.Brightness(im).enhance(rng.uniform(0.85, 1.15))
        s2 = rng.uniform(0.85, 1.0)
        return im.resize((max(32, int(im.width * s2)), max(32, int(im.height * s2))))

    _ = colorsys  # hue shift is done in PIL HSV space
    return f"creative_recolor:{_edit_top_images(root, rng, fn)}"


def v_creative_overlay(root: Path, rng: random.Random) -> str | None:
    if not _creative_only(root):
        return None
    from PIL import ImageDraw

    def fn(im):
        im = im.copy()
        d = ImageDraw.Draw(im)
        bh = int(im.height * rng.uniform(0.12, 0.18))
        top = rng.random() < 0.5
        y0 = 0 if top else im.height - bh
        d.rectangle([0, y0, im.width, y0 + bh], fill=tuple(rng.randint(0, 255) for _ in range(3)))
        d.text((10, y0 + bh // 3), f"{rng.choice(['PILNE', 'NOWOŚĆ', 'SPRAWDŹ'])} {rng.randint(10, 99)}%",
               fill=(255, 255, 255))
        return im

    return f"creative_overlay:{_edit_top_images(root, rng, fn)}"


def _paraphrase_text(text: str, rng: random.Random) -> str:
    def num(m: re.Match) -> str:
        s = m.group(0)
        return str(rng.randint(1, 9)) + "".join(str(rng.randint(0, 9)) for _ in s[1:])

    text = re.sub(r"\d{2,}", num, text)
    for a, b in SYN_PL.items():
        text = re.sub(rf"\b{re.escape(a)}\b", b, text)
    words = re.findall(r"\b[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]{4,}\b", text)
    for w in words[: max(1, len(words) // 3)]:
        text = text.replace(w, rng.choice(FICTIONAL_NAMES), 1)
    sents = re.split(r"(?<=[.!?])\s+", text)
    if len(sents) >= 3:
        i = rng.randrange(len(sents) - 1)
        sents[i], sents[i + 1] = sents[i + 1], sents[i]
        text = " ".join(sents)
    return text


def v_paraphrase(root: Path, rng: random.Random) -> str | None:
    from bs4 import BeautifulSoup, NavigableString

    pages = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".html", ".htm"}]
    if not pages:
        return None
    n = 0
    for p in pages:
        soup = BeautifulSoup(p.read_text(encoding="utf-8", errors="replace"), "lxml")
        for s in list(soup.find_all(string=True)):
            if (isinstance(s, NavigableString) and s.parent is not None
                    and s.parent.name not in ("script", "style", "noscript") and len(s.strip()) > 20):
                s.replace_with(_paraphrase_text(str(s), rng))
                n += 1
        p.write_text(str(soup), encoding="utf-8")
    dom = m_new_domain(root, rng)
    m_drop_har(root, rng)
    return f"paraphrase:{n}:{dom}"


def v_newdom_kit(root: Path, rng: random.Random) -> str | None:
    log = m_new_domain(root, rng)
    return log if log != "new_domain:" else None


def _chrome() -> Path | None:
    hits = sorted(Path.home().glob(CHROME_GLOB))
    return hits[-1] if hits else None


def v_landing_shot(root: Path, rng: random.Random) -> str | None:
    html, chrome = _main_html(root), _chrome()
    if html is None or chrome is None:
        return None
    shot = root.parent / f".{root.name}-shot.png"
    cmd = [str(chrome), "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
           "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-background-networking", "--no-first-run",
           f"--window-size={rng.choice([1280, 1366, 1440])},{rng.choice([1800, 2200])}",
           "--virtual-time-budget=4000", f"--screenshot={shot}", html.resolve().as_uri()]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        return None
    if not shot.is_file() or shot.stat().st_size < 5_000:
        return None
    for p in list(root.iterdir()):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.move(shot, root / "zrzut_strony.png")
    return "landing_shot"


def v_html_no_har(root: Path, rng: random.Random) -> str | None:
    hs = [p for p in _files(root) if _is_har(p)]
    if not hs or _main_html(root) is None:
        return None
    for p in hs:
        p.unlink()
    return f"html_no_har:{len(hs)}"


def v_html_only(root: Path, rng: random.Random) -> str | None:
    html = _main_html(root)
    if html is None:
        return None
    keep = [p for p in _files(root) if p.suffix.lower() in {".html", ".htm"}
            or any(part.endswith("_files") for part in p.relative_to(root).parts[:-1])]
    _keep_only(root, keep)
    return f"html_only:{len(keep)}"


def v_no_meta(root: Path, rng: random.Random) -> str | None:
    meta = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".json", ".txt", ".csv"}]
    for p in meta:
        p.unlink()
    k = 0
    for p in sorted(root.iterdir()):
        if p.is_file() and p.suffix.lower() in IMG_EXT | VID_EXT:
            k += 1
            p.rename(p.with_name(f"plik{k}{p.suffix.lower()}"))
    return f"no_meta:{len(meta)}:{k}" if meta or k else None


def v_video_reenc(root: Path, rng: random.Random) -> str | None:
    vids = [p for p in _files(root) if p.suffix.lower() in VID_EXT]
    if not vids:
        return None
    src = max(vids, key=lambda p: p.stat().st_size)
    out = root.parent / f".{root.name}-video.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-threads", "2", "-ss", "1", "-i", str(src), "-vf",
           "scale=trunc(iw*0.6/2)*2:trunc(ih*0.6/2)*2", "-c:v", "libx264", "-crf", "30", "-preset", "veryfast",
           "-c:a", "aac", "-b:a", "64k", str(out)]
    r = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    if r.returncode != 0 or not out.is_file():
        return None
    for p in list(root.iterdir()):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.move(out, root / "nagranie.mp4")
    return "video_reenc"


POSITIVE = {
    "dup_exact": lambda root, rng: "dup_exact", "img5_any": v_img5_any, "img3_top": v_img3_top,
    "creative1": v_creative1, "creative_crop": v_creative_crop, "creative_recolor": v_creative_recolor,
    "creative_overlay": v_creative_overlay, "paraphrase": v_paraphrase, "newdom_kit": v_newdom_kit,
    "landing_shot": v_landing_shot, "html_no_har": v_html_no_har, "html_only": v_html_only, "no_meta": v_no_meta,
    "video_reenc": v_video_reenc,
}


# --------------------------------------------------------------------------------------- negatives


def _ext_dirs(ext: Path, prefix: str) -> list[Path]:
    return sorted(d for d in ext.iterdir() if d.is_dir() and d.name.startswith(prefix + "__"))


def _brand_dirs(ext: Path) -> list[Path]:
    out = []
    for d in sorted(x for x in ext.iterdir() if x.is_dir()):
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in {".html", ".txt", ".htm"} and f.stat().st_size < 5_000_000:
                if BRAND_GREP.search(f.read_text(encoding="utf-8", errors="ignore")):
                    out.append(d)
                    break
    return out


def _photo_pool(ext: Path) -> list[Path]:
    return sorted(p for d in _ext_dirs(ext, "purino-scam-ads") for p in d.iterdir()
                  if p.suffix.lower() in IMG_EXT and p.stat().st_size > 15_000)


def n_brand_creative(dst: Path, rng: random.Random, photos: list[Path]) -> str:
    from PIL import Image, ImageDraw

    dst.mkdir(parents=True)
    base = Image.open(rng.choice(photos)).convert("RGB").resize((1080, 1080))
    d = ImageDraw.Draw(base)
    person, brand = rng.choice(BRAND_PERSONS), rng.choice(BRAND_MEDIA)
    d.rectangle([0, 0, 1080, 170], fill=(200, 0, 0))
    d.text((30, 40), f"{brand} NA ŻYWO", fill=(255, 255, 255))
    d.text((30, 100), f"{person}: to zmieniło wszystko. Sprawdź, zanim usuną!", fill=(255, 255, 255))
    d.rectangle([0, 960, 1080, 1080], fill=(20, 20, 20))
    d.text((30, 1000), f"Zarabiaj od {rng.randint(3, 9)} 000 zł miesięcznie", fill=(255, 255, 0))
    _save_jpeg(base, dst / "reklama.jpg", rng.randint(70, 90))
    (dst / "tekst_reklamy.txt").write_text(
        f"{person} ujawnia sposób na dodatkowy dochód. {brand} potwierdza. "
        f"Zarejestruj się dziś na {rng.choice(WORDS)}{rng.choice(WORDS)}.com", encoding="utf-8")
    return f"brand_creative:{person}/{brand}"


def n_generic_lead(dst: Path, rng: random.Random) -> str:
    dst.mkdir(parents=True)
    host = f"{rng.choice(WORDS)}-{rng.choice(WORDS)}{rng.randint(1, 99)}.{rng.choice(['shop', 'online', 'site', 'pl'])}"
    pixel = "".join(str(rng.randint(0, 9)) for _ in range(16))
    product = rng.choice(["Flexoril", "Cardioxin", "Visiolux", "Slimgard", "Hondrex"])
    html = f"""<!DOCTYPE html><html lang="pl"><head><meta charset="utf-8"><title>{product} - oficjalna strona</title>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
<script>!function(f,b,e,v,n,t,s){{n=f.fbq=function(){{}};t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}}(window,document,'script',
'https://connect.facebook.net/en_US/fbevents.js');fbq('init','{pixel}');fbq('track','PageView');</script>
</head><body><div class="container"><h1>{product}</h1><p>Tylko dziś -50%! Cena {rng.randint(79, 199)} zł.
Zostało {rng.randint(5, 25)} sztuk.</p><form action="order.php" method="post" class="order-form">
<input name="name" placeholder="Imię"><input name="phone" placeholder="Telefon">
<input type="hidden" name="sub1" value="{{sub1}}"><button type="submit">Zamów</button></form>
<div class="comments"><b>Jan Kowalski</b> Polecam! <b>Anna Nowak</b> Działa.</div></div></body></html>"""
    (dst / f"{product}.html").write_text(html, encoding="utf-8")
    har = {"log": {"version": "1.2", "creator": {"name": "WebInspector", "version": "537.36"}, "entries": [
        {"request": {"method": "GET", "url": f"https://{host}/?utm_source=facebook&utm_campaign={{campaign.name}}"
                     "&fbclid=IwAR0", "headers": []},
         "response": {"status": 200, "redirectURL": "", "headers": [{"name": "server", "value": "cloudflare"}]},
         "serverIPAddress": f"104.21.{rng.randint(1, 99)}.{rng.randint(1, 254)}"},
        {"request": {"method": "POST", "url": f"https://{host}/order.php", "headers": [],
                     "postData": {"mimeType": "application/x-www-form-urlencoded", "text": "name=x&phone=y&sub1=z"}},
         "response": {"status": 302, "redirectURL": f"https://{host}/success.html", "headers": []},
         "serverIPAddress": f"104.21.{rng.randint(1, 99)}.{rng.randint(1, 254)}"}]}}
    (dst / f"{host}.har").write_text(json.dumps(har), encoding="utf-8")
    return f"generic_lead:{product}"


def _public_urls() -> list[str]:
    p = HOME / "data" / "public-corpus" / "pl-feeds" / "groups.jsonl"
    urls = []
    if p.is_file():
        for line in p.open(encoding="utf-8"):
            row = json.loads(line)
            for r in row.get("rows", []):
                if not r.get("url_synthesised"):
                    urls.append(r["url"])
    return urls


# --------------------------------------------------------------------------------------- make


def make(out: Path, seed: int, sample: Path, ext: Path, neg_per_type: int = 6) -> list[dict]:
    rng = random.Random(seed)
    if out.exists():
        shutil.rmtree(out)
    new = out / "new"
    new.mkdir(parents=True)
    rows: list[dict] = []
    k = 0

    def item() -> str:
        nonlocal k
        k += 1
        return f"pozycja-{k:03d}"

    for src in sorted(p for p in sample.iterdir() if p.is_dir()):
        iid = f"nask-{src.name}"
        for vt in POSITIVE_TYPES:
            name = item()
            dst = new / name
            if vt == "zip":
                with zipfile.ZipFile(new / f"{name}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in _files(src):
                        zf.write(p, Path(src.name) / p.relative_to(src))
                rows.append({"item": f"{name}.zip", "expect": f"known:{iid}", "variant": vt, "source": iid,
                             "log": "zip"})
                continue
            shutil.copytree(src, dst)
            try:
                log = POSITIVE[vt](dst, rng)
            except Exception as exc:  # noqa: BLE001 -- a failing generator is reported, not fatal
                log = None
                print(f"{name} {vt} failed: {type(exc).__name__}")
            if not log or not any(dst.rglob("*")):
                shutil.rmtree(dst, ignore_errors=True)
                k -= 1
                continue
            rows.append({"item": name, "expect": f"known:{iid}", "variant": vt, "source": iid, "log": log})

    def add_neg(vt: str, src_name: str, fn) -> None:
        name = item()
        try:
            log = fn(new / name)
        except Exception as exc:  # noqa: BLE001
            print(f"{name} {vt} failed: {type(exc).__name__}: {exc}")
            shutil.rmtree(new / name, ignore_errors=True)
            return
        if log:
            rows.append({"item": name, "expect": "new", "variant": vt, "source": src_name, "log": log})

    brand = _brand_dirs(ext)
    used: set[str] = set()
    for vt, pool in (("ext_brand", brand), ("ext_purino", _ext_dirs(ext, "purino-scam-ads")),
                     ("ext_phresh", _ext_dirs(ext, "phreshphish"))):
        pool = [d for d in pool if d.name not in used]
        for d in rng.sample(pool, min(neg_per_type + (4 if vt == "ext_brand" else 0), len(pool))):
            used.add(d.name)
            add_neg(vt, f"external:{d.name.split('__')[0]}",
                    lambda dst, d=d: shutil.copytree(d, dst) and f"copy:{d.name.split('__')[0]}")
    for src in rng.sample(sorted(p for p in sample.iterdir() if p.is_dir() and _main_html(p)), 4):
        add_neg("tpl_clone", f"template:nask-{src.name}",
                lambda dst, src=src: (template_clone(src, dst, rng) or [None])[0])
    photos = _photo_pool(ext)
    for _ in range(neg_per_type):
        add_neg("brand_creative", "synthetic", lambda dst: n_brand_creative(dst, rng, photos))
    for _ in range(neg_per_type):
        add_neg("generic_lead", "synthetic", lambda dst: n_generic_lead(dst, rng))
    urls = _public_urls()
    for u in rng.sample(urls, min(neg_per_type, len(urls))):
        def write(dst: Path, u: str = u) -> str:
            dst.mkdir(parents=True)
            (dst / "zgloszenie.txt").write_text(f"Podejrzany link z reklamy: {u}\n", encoding="utf-8")
            return "url_only"
        add_neg("url_only", "public:pl-feeds", write)

    with (out / "truth.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["item", "expect", "variant", "source", "log"])
        w.writeheader()
        w.writerows(rows)
    (out / "README.txt").write_text("SYNTHETIC hidden-set simulation (eval/hidden_sim.py); derived from organiser "
                                    "material: server only, never in git.\n", encoding="utf-8")
    return rows


# --------------------------------------------------------------------------------------- score


def campaign_of(state: Path, run: str | None = None) -> tuple[str, dict[str, str]]:
    from contextlib import closing

    from hydra.config import Settings
    from hydra.db import connect
    from hydra.pipeline import latest_run, load_clustering

    s = Settings(state_dir=state)
    with closing(connect(s.db_path, readonly=True)) as conn:
        run = run or latest_run(conn)
        camps, _groups = load_clustering(conn, run)
    return run, {m: c.id for c in camps for m in c.members}


def judge(rows: list[dict], truth: dict[str, dict], member: dict[str, str]) -> list[dict]:
    out = []
    for item, t in truth.items():
        r = next((x for x in rows if x["item"] == item), None)
        dec = (r or {}).get("decision") or "missing"
        cid = (r or {}).get("campaign_id")
        if t["expect"] == "new":
            outcome = {"new_campaign": "ok_new", "abstain": "abstain", "assign": "false_assign"}.get(dec, dec)
        else:
            want = member.get(t["expect"].split(":", 1)[1])
            outcome = ("ok" if dec == "assign" and cid == want else "wrong_campaign" if dec == "assign"
                       else {"abstain": "abstain", "new_campaign": "missed_new"}.get(dec, dec))
        out.append({**t, "decision": dec, "campaign_id": cid, "outcome": outcome,
                    "top_bits": (r or {}).get("top_bits"), "incident_id": (r or {}).get("incident_id")})
    return out


def summarise(judged: list[dict]) -> dict:
    per: dict[str, Counter] = defaultdict(Counter)
    for j in judged:
        per[j["variant"]][j["outcome"]] += 1
    pos = [j for j in judged if j["expect"] != "new"]
    neg = [j for j in judged if j["expect"] == "new"]
    agg = {
        "positives": len(pos), "negatives": len(neg),
        "assign_accuracy": round(sum(j["outcome"] == "ok" for j in pos) / max(1, len(pos)), 3),
        "pos_abstain_rate": round(sum(j["outcome"] == "abstain" for j in pos) / max(1, len(pos)), 3),
        "pos_missed_new_rate": round(sum(j["outcome"] == "missed_new" for j in pos) / max(1, len(pos)), 3),
        "pos_wrong_campaign": sum(j["outcome"] == "wrong_campaign" for j in pos),
        "openset_rejection_strict": round(sum(j["outcome"] == "ok_new" for j in neg) / max(1, len(neg)), 3),
        "openset_rejection_incl_abstain": round(sum(j["outcome"] in ("ok_new", "abstain") for j in neg)
                                                / max(1, len(neg)), 3),
        "neg_false_assign": sum(j["outcome"] == "false_assign" for j in neg),
        "false_assign_rate": round((sum(j["outcome"] == "false_assign" for j in neg)
                                    + sum(j["outcome"] == "wrong_campaign" for j in pos)) / max(1, len(judged)), 3),
        "errors": sum(j["outcome"] in ("error", "missing") for j in judged),
    }
    return {"aggregate": agg, "per_variant": {k: dict(v) for k, v in sorted(per.items())}}


def print_table(summary: dict) -> None:
    cols = ["ok", "ok_new", "abstain", "missed_new", "wrong_campaign", "false_assign", "error", "missing"]
    print(f"{'variant':<18}" + "".join(f"{c:>15}" for c in cols) + f"{'n':>5}")
    for v, c in summary["per_variant"].items():
        print(f"{v:<18}" + "".join(f"{c.get(x, 0):>15}" for x in cols) + f"{sum(c.values()):>5}")
    print(json.dumps(summary["aggregate"], indent=1))


def load_truth(path: Path) -> dict[str, dict]:
    return {r["item"]: r for r in csv.DictReader(path.open(encoding="utf-8"))}


def rescore(state: Path, rows: list[dict], run: str | None = None) -> list[dict]:
    """Re-decide every assigned item with the CURRENT scoring code on the genes already stored in `state` by
    `hydra assign-batch` (product path `pipeline.assign_stored`; no re-extraction)."""
    from hydra.config import Settings
    from hydra.pipeline import assign_stored

    s = Settings(state_dir=state)
    out = []
    for r in rows:
        if not r.get("incident_id"):
            out.append(r)
            continue
        res = assign_stored(r["incident_id"], s, base_run=run or r.get("base_run"))
        a = res.assignment
        top = a.candidates[0] if a.candidates else None
        out.append({**r, "decision": str(a.decision), "campaign_id": a.campaign_id, "group_id": a.group_id,
                    "top_bits": top.best_pair.total_bits if top and top.best_pair else None,
                    "reason_pl": a.reason_pl})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--seed", type=int, default=1)
    m.add_argument("--sample", type=Path, default=HOME / "data" / "nask" / "sample")
    m.add_argument("--ext", type=Path, default=HOME / "data" / "external-big" / "corpus-demo")
    m.add_argument("--neg-per-type", type=int, default=6)
    for name in ("score", "rescore"):
        p = sub.add_parser(name)
        p.add_argument("--truth", type=Path, required=True)
        p.add_argument("--assignments", type=Path, nargs="+", required=True)
        p.add_argument("--state", type=Path, nargs="+", required=True,
                       help="state dir(s) of the run(s), in the order of --assignments")
        p.add_argument("--run", default=None)
        p.add_argument("--json", type=Path, default=None)
        p.add_argument("--details", action="store_true", help="print item, variant, outcome, top bits (no content)")
    a = ap.parse_args()
    if a.cmd == "make":
        rows = make(a.out, a.seed, a.sample, a.ext, a.neg_per_type)
        c = Counter(r["variant"] for r in rows)
        print(json.dumps({"items": len(rows), "by_variant": dict(sorted(c.items()))}, indent=1))
        return
    truth = load_truth(a.truth)
    judged_all: list[dict] = []
    member: dict[str, str] = {}
    for path, state in zip(a.assignments, a.state * (len(a.assignments) if len(a.state) == 1 else 1), strict=False):
        rows = json.loads(path.read_text(encoding="utf-8"))
        if a.cmd == "rescore":
            rows = rescore(state, rows, a.run)
        _run, member = campaign_of(state, a.run)
        names = {r["item"] for r in rows}
        judged_all += judge(rows, {k: v for k, v in truth.items() if k in names}, member)
    missing = {k: v for k, v in truth.items() if k not in {j["item"] for j in judged_all}}
    judged_all += judge([], missing, member)
    summary = summarise(judged_all)
    print_table(summary)
    if a.details:
        for j in judged_all:
            if j["outcome"] not in ("ok", "ok_new"):
                print(f"  {j['item']:<14} {j['variant']:<16} {j['source']:<28} {j['outcome']:<15} {j['top_bits']}")
    if a.json:
        a.json.write_text(json.dumps({"summary": summary, "items": judged_all}, indent=1, ensure_ascii=False,
                                     default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
