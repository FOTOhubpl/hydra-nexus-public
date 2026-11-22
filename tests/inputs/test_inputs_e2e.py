"""Every input kind of the NASK brief end to end through `analyse_input` (the one dispatcher behind the CLI and
the API), with the real extractors that need no model weights. Synthetic material only, generated here.

Corpus: campaign A (3 landing pages on pko-bonus24.com, one phone, one IBAN, one pixel), campaign B (2 pages on
tani-buty24.pl) and 10 unrelated fillers, so rarity bits are meaningful.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from PIL import Image

from hydra.config import Settings
from hydra.db import connect
from hydra.genome import base
from hydra.genome.webparse import pii_hash  # operator-genes: account numbers are stored as salted hashes
from hydra.pipeline import analyse, analyse_input

base.load_family_modules()
ONLY = ["inf_url", "inf_har", "inf_dom", "txt_lines", "txt_phrases", "txt_entities", "txt_chat", "nar", "meta",
        "artifact_sha256", "vis"]
PHONE_A = "+48 512 734 981"  # merge-batch3: a mobile number; operator-genes tiers 700 100 100 as boilerplate
IBAN_A = "PL61109010140000071219812874"
PIXEL_A = "<script>fbq('init','987654321012345');</script>"
TEXT_A = "Inwestuj z PKO i zarabiaj 5000 zł dziennie. Zadzwoń do doradcy"


def page(d: Path, url: str, body: str, title: str = "Oferta") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "Strona.html").write_text(
        f"<!-- saved from url=(0060){url} -->\n<html><head><title>{title}</title>{PIXEL_A if 'pko' in url else ''}"
        f"</head><body><h1>{title}</h1><p>{body}</p></body></html>", encoding="utf-8")


def _settings(root: Path) -> Settings:
    s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                 state_dir=root / "state", max_file_mb=5)
    s.ensure_dirs()
    return s


@pytest.fixture(scope="module")
def _corpus_template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("inputs-corpus")
    settings = _settings(root / "env")
    ds = root / "ds"
    for i in (1, 2, 3):
        page(ds / str(i), f"https://pko-bonus24.com/oferta/{100 + i}?utm_source=fb",
             f"{TEXT_A} {PHONE_A}. Przelew na {IBAN_A}.", "Inwestycja PKO Bonus")
    for i in (4, 5):
        page(ds / str(i), f"https://tani-buty24.pl/sklep/{i}", "Wyprzedaż butów -90%. Zadzwoń +48 600 200 300",
             "Buty wyprzedaż")
    for i in range(6, 66):
        page(ds / str(i), f"https://filler{i}-site.com/p/{i}", f"Zupełnie inna treść numer {i} o ogrodzie",
             f"Ogród {i}")
    analyse([ds], settings, only=ONLY)
    return root / "env"


@pytest.fixture
def corpus(_corpus_template: Path, tmp_path: Path) -> Settings:
    """A private copy of the analysed corpus (state + caches) per test: tests never see each other's inputs."""
    import shutil

    shutil.copytree(_corpus_template, tmp_path / "env")
    return _settings(tmp_path / "env")


def genes_of(settings: Settings, iid: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    with connect(settings.db_path, readonly=True) as conn:
        for r in conn.execute("SELECT kind, value FROM genes WHERE incident_id=? AND status='observed'", (iid,)):
            out.setdefault(r["kind"], set()).add(r["value"])
    return out


def campaign_of(settings: Settings, member: str) -> str:
    with connect(settings.db_path, readonly=True) as conn:
        return conn.execute("SELECT campaign_id FROM campaign_members WHERE incident_id=? ORDER BY rowid DESC",
                            (member,)).fetchone()[0]


def run(kind: str, payload, settings: Settings) -> dict:
    t = time.perf_counter()
    out = analyse_input(kind, payload, settings, only=ONLY)
    out["_seconds"] = time.perf_counter() - t
    if kind != "hint":
        a = out["assignment"]
        assert a is None or (a["decision"] in ("assign", "new_campaign", "abstain") and a["reason_pl"]), out
    return out


# --------------------------------------------------------------------------------------------------- 1. URL


def test_url_offline_gives_url_genes_status_and_assignment(corpus: Settings):
    out = run("url", "https://pko-bonus24.com/oferta/999?utm_source=fb", corpus)
    assert out["fetch"]["status"] == "unavailable"
    assert "niedostępna offline" in out["fetch"]["status_pl"] and out["fetch"]["status_pl"] in out["notes"]
    g = genes_of(corpus, out["incident_id"])
    assert g["landing_host"] == {"pko-bonus24.com"} and g["registrable_domain"] == {"pko-bonus24.com"}
    assert "/oferta/{num}" in g["url_path_skeleton"] and "url_label_skeleton" in g
    assert any(v.startswith("PKO BP|") for v in g["brand_lookalike"])  # typosquat vs the public brand list
    a = out["assignment"]
    # only the known campaign's domain is observable: below the link threshold the campaign is named as the best
    # candidate (and its kit), never assigned on a bare URL; open-set rejection stays as tuned (sample regression)
    assert a["decision"] in ("assign", "abstain", "new_campaign"), a
    assert a["candidates"][0]["campaign_id"] == campaign_of(corpus, "nask-1")
    assert campaign_of(corpus, "nask-1") in a["reason_pl"]
    unknown = run("url", "https://zupelnie-nowa-domena.com/start", corpus)
    assert unknown["assignment"]["decision"] == "new_campaign"


def test_url_homoglyph_and_digit_skeleton(corpus: Settings):
    out = run("url", "https://pkо-bonus24.com/oferta/1", corpus)  # Cyrillic 'о'
    g = genes_of(corpus, out["incident_id"])
    assert g["landing_host"] == {"xn--pk-bonus24-fvi.com"}
    assert g["host_folded"] == {"pko-bonus24.com"} and "idn_host" in g
    assert any("|homoglyph|" in v for v in g["brand_lookalike"])
    out2 = run("url", "https://allegrolokalnie.pe-7492104.sbs/oferta/123", corpus)
    assert "allegrolokalnie.pe-#.sbs" in genes_of(corpus, out2["incident_id"])["url_digit_skeleton"]


LIVE_PAGE = (f"<html><head><title>Inwestycja PKO Bonus</title>{PIXEL_A}</head><body><h1>Inwestycja PKO Bonus</h1>"
             f"<p>{TEXT_A} {PHONE_A}. Przelew na {IBAN_A}.</p></body></html>").encode()


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/start"):
            code, hdrs, body = 302, {"Location": "/oferta/7", "Set-Cookie": "sid=1"}, b""
        elif self.path.startswith("/oferta/"):
            code, hdrs, body = 200, {"Content-Type": "text/html; charset=utf-8"}, LIVE_PAGE
        else:
            code, hdrs, body = 404, {}, b"nope"
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_url_live_fetch_is_isolated_stored_and_replayed(corpus: Settings, _corpus_template: Path,
                                                        tmp_path: Path, monkeypatch):
    """HYDRA_ENRICH=1 path with a loopback server standing in for the internet (the SSRF guard is patched)."""
    from hydra.enrich import fetch

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(fetch, "_public_ip", lambda host, port: "127.0.0.1")
        url = f"http://127.0.0.1:{srv.server_port}/start"
        live = corpus.model_copy(update={"enrich": True, "offline": False})
        out = run("url", url, live)
        f = out["fetch"]
        assert f["status"] == "observed" and f["hops"] == 2 and len(f["redirect_chain"]) == 2, f
        assert f["screenshot"] == "not_available"  # no sandboxed headless browser: nothing rendered, no JS
        with connect(corpus.db_path, readonly=True) as conn:
            roles = {r["path_in_incident"]: r["role"] for r in conn.execute(
                "SELECT path_in_incident, role FROM incident_artifacts WHERE incident_id=?", (out["incident_id"],))}
            assert roles.get("landing.html") == "landing_html" and roles.get("fetch.har") == "har"  # CAS evidence
        g = genes_of(corpus, out["incident_id"])
        assert "+48512734981" in g["entity_phone"] and pii_hash(IBAN_A) in g["entity_iban"]
        assert "fb_pixel:987654321012345" in g["tracker_id"]
        assert out["assignment"]["decision"] == "assign", out["assignment"]["reason_pl"]
        assert out["assignment"]["campaign_id"] == campaign_of(corpus, "nask-1")
    finally:
        srv.shutdown()
    # replay: the server is gone and live lookups are off, the cached fetch gives the same page
    replay = run("url", url, corpus)
    assert replay["fetch"]["status"] == "observed"
    assert replay["fetch"]["replayed"] and replay["fetch"]["hops"] == 2  # chain + page rebuilt from the cached HAR
    assert replay["assignment"]["decision"] == "assign"
    # a fresh corpus copy that has only the fetch cache (the evaluation/demo case): same page, same decision
    import shutil

    shutil.copytree(_corpus_template, tmp_path / "env2")
    fresh = _settings(tmp_path / "env2")
    shutil.copytree(corpus.enrich_cache / "http_fetch", fresh.enrich_cache / "http_fetch")
    again = run("url", url, fresh)
    assert again["fetch"]["replayed"] and again["assignment"]["decision"] == "assign", again["assignment"]["reason_pl"]
    assert "fb_pixel:987654321012345" in genes_of(fresh, again["incident_id"])["tracker_id"]


# ---------------------------------------------------------------------------------------------- 2. screenshot


def _shot(tmp_path: Path, shade: int = 250) -> Path:
    p = tmp_path / "zrzut.png"
    Image.new("RGB", (640, 200), (shade, shade, shade)).save(p)  # distinct bytes per test (OCR is memoised by sha)
    return p


def test_screenshot_ocr_url_is_followed_offline(corpus: Settings, tmp_path: Path, monkeypatch):
    from hydra.genome import txt_sources
    from hydra.genome.ocr_pool import OcrPool
    from hydra.ml.ocr import OcrLine

    lines = [OcrLine("Zarabiaj z PKO: https://pko-bonus24.com/oferta/55", (10, 10, 600, 40), 0.95, "fake"),
             OcrLine(f"Zadzwoń {PHONE_A}", (10, 60, 400, 90), 0.95, "fake")]
    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    monkeypatch.setattr(txt_sources, "shared_pool",
                        lambda *a, **k: OcrPool(corpus.cache_dir, engine_fn=lambda im: lines))
    out = run("screenshot", _shot(tmp_path), corpus)
    assert out["urls_in_image"][0] == "https://pko-bonus24.com/oferta/55"  # full URL before the bare domain
    assert "+48512734981" in out["phones_in_image"]
    assert out["fetch"]["status"] == "unavailable" and any("OCR" in n for n in out["notes"])
    g = genes_of(corpus, out["incident_id"])
    assert g["landing_host"] == {"pko-bonus24.com"} and "/oferta/{num}" in g["url_path_skeleton"]
    assert out["assignment"]["decision"] in ("assign", "abstain")


REAL_OCR = Path.home() / "HydraNexus" / "models" / "rapidocr-v6"


@pytest.mark.skipif(not REAL_OCR.is_dir(), reason="RapidOCR weights not installed (just models-fetch)")
def test_screenshot_real_ocr(corpus: Settings, tmp_path: Path, monkeypatch):
    from PIL import ImageDraw, ImageFont

    monkeypatch.setenv("HYDRA_MODELS_DIR", str(REAL_OCR.parent))
    im = Image.new("RGB", (900, 220), "white")
    dr = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=36)
    dr.text((20, 30), "Zarabiaj: https://pko-bonus24.com/oferta/77", fill="black", font=font)
    dr.text((20, 120), "Zadzwon +48 512 734 981", fill="black", font=font)
    p = tmp_path / "real.png"
    im.save(p)
    out = run("screenshot", p, corpus.model_copy(update={"models_dir": REAL_OCR.parent}))
    assert any("pko-bonus24.com" in u for u in out["urls_in_image"]), (out["urls_in_image"], out["notes"])
    assert "pko-bonus24.com" in genes_of(corpus, out["incident_id"])["landing_host"]


# --------------------------------------------------------------------------------------------------- 3. hint


def test_hint_ranks_campaign_with_reasons(corpus: Settings):
    out = run("hint", "PKO bonus", corpus)["search"]
    top = out["incidents"][0]
    assert top["incident_id"] in {"nask-1", "nask-2", "nask-3"} and set(top["terms_covered"]) == {"pko", "bonus"}
    assert top["matches"] and all(m["kind"] and m["bits"] > 0 for m in top["matches"])
    assert out["campaigns"][0]["campaign_id"] == campaign_of(corpus, "nask-1")
    assert run("hint", "Żółć", corpus)["search"]["incidents"] == []  # no false hit


# ------------------------------------------------------------------------------------------ 4. files / logs

WA = (f"21.11.2026, 14:05 - Doradca Marek: Dzień dobry, {TEXT_A}\n"
      "21.11.2026, 14:06 - Jan Kowalski: Jak zacząć?\n"
      f"21.11.2026, 14:07 - Doradca Marek: Wpłać 1200 zł na {IBAN_A}\n"
      f"wejdź na https://pko-bonus24.com/oferta/5 i dzwoń {PHONE_A}\n"
      "21.11.2026, 14:08 - Jan Kowalski: <Pominięto multimedia>\n")


def test_whatsapp_log_parsed_into_messages_and_genes(corpus: Settings, tmp_path: Path):
    from hydra.genome.chat import parse_chat

    p = tmp_path / "WhatsApp Chat - Doradca.txt"
    p.write_text(WA, encoding="utf-8")
    log = parse_chat(p)
    assert log.platform == "whatsapp" and len(log.messages) == 3  # media placeholder dropped, continuation joined
    assert log.senders == ["Doradca Marek", "Jan Kowalski"] and log.span == ("2026-11-21T14:05:00",
                                                                                "2026-11-21T14:07:00")
    out = run("file", p, corpus)
    g = genes_of(corpus, out["incident_id"])
    assert {"doradca marek", "jan kowalski"} <= g["chat_persona"]
    assert g["chat_start_date"] == {"2026-11-21"} and g["chat_platform"] == {"whatsapp"}
    assert "+48512734981" in g["entity_phone"] and pii_hash(IBAN_A) in g["entity_iban"]
    assert "1200 PLN" in g["entity_price"]
    assert "pko-bonus24.com" in g["landing_host"]  # URL in a message → INF URL genes
    assert not any("14:05" in v for v in g.get("phrase", set()))  # phrases from bodies, not timestamp prefixes
    assert out["assignment"]["decision"] == "assign", out["assignment"]["reason_pl"]
    assert out["input_report"]["files"][0]["status"] == "ok"


def test_whatsapp_ios_cp1250_same_genes(corpus: Settings, tmp_path: Path):
    ios = "\n".join(f"[{d}, {t}:00] {s}: {m}" for d, t, s, m in (
        ("21.11.2026", "14:05", "Doradca Marek", f"Dzień dobry, {TEXT_A}"),
        ("21.11.2026", "14:07", "Doradca Marek", f"Wpłać na {IBAN_A}, tel {PHONE_A}")))
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text(ios, encoding="utf-8")
    b.write_bytes(ios.encode("cp1250"))
    ga = genes_of(corpus, run("file", a, corpus)["incident_id"])
    gb = genes_of(corpus, run("file", b, corpus)["incident_id"])
    for k in ("chat_persona", "entity_iban", "entity_phone", "phrase"):
        assert ga[k] == gb[k], k  # same content in another encoding → the same genes


def test_telegram_and_messenger_json(corpus: Settings, tmp_path: Path):
    tg = tmp_path / "result.json"
    tg.write_text(json.dumps({"name": "Inwestycje", "type": "personal_chat", "messages": [
        {"id": 1, "type": "service", "date": "2026-11-20T10:00:00", "actor": "X", "action": "create_group"},
        {"id": 2, "type": "message", "date": "2026-11-20T10:01:00", "from": "Anna Inwestor", "text": TEXT_A},
        {"id": 3, "type": "message", "date": "2026-11-20T10:02:00", "from": "Anna Inwestor",
         "text": [{"type": "link", "text": "https://pko-bonus24.com/oferta/8"}, f" albo dzwoń {PHONE_A}"]}]}),
        encoding="utf-8")
    g = genes_of(corpus, run("file", tg, corpus)["incident_id"])
    assert g["chat_platform"] == {"telegram"} and g["chat_persona"] == {"anna inwestor"}
    assert "pko-bonus24.com" in g["landing_host"] and "+48512734981" in g["entity_phone"]
    moj = "Zarabiaj 5000 zł".encode().decode("latin-1")  # Facebook's mojibake
    ms = tmp_path / "message_1.json"
    ms.write_text(json.dumps({"participants": [{"name": "Piotr Doradca"}, {"name": "Ola"}], "messages": [
        {"sender_name": "Piotr Doradca", "timestamp_ms": 1795262400000, "content": moj},
        {"sender_name": "Ola", "timestamp_ms": 1795262460000, "content": "Gdzie?"},
        {"sender_name": "Piotr Doradca", "timestamp_ms": 1795262520000, "content": "Tutaj",
         "share": {"link": "https://pko-bonus24.com/oferta/9"}}]}), encoding="utf-8")
    g = genes_of(corpus, run("file", ms, corpus)["incident_id"])
    assert g["chat_platform"] == {"messenger"} and "piotr doradca" in g["chat_persona"]
    assert "5000 PLN" in g["entity_price"] and "pko-bonus24.com" in g["landing_host"]


def _pdf(text: str, uri: str) -> bytes:
    content = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/Contents 4 0 R/Annots[5 0 R]>>endobj\n"
            b"4 0 obj<</Length " + str(len(content)).encode() + b"/Filter/FlateDecode>>stream\n" + content
            + b"\nendstream endobj\n5 0 obj<</Type/Annot/Subtype/Link/A<</S/URI/URI(" + uri.encode()
            + b")>>>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")


def test_ad_files_pdf_html_image_video_zip(corpus: Settings, tmp_path: Path, make_png):
    pdf = tmp_path / "reklama.pdf"
    pdf.write_bytes(_pdf(f"Inwestuj z PKO, zadzwon {PHONE_A}", "https://pko-bonus24.com/oferta/3"))
    out = run("file", pdf, corpus)
    g = genes_of(corpus, out["incident_id"])
    assert "+48512734981" in g["entity_phone"] and "pko-bonus24.com" in g["landing_host"], g
    html = tmp_path / "ad.html"
    html.write_text(f"<!-- saved from url=(0040)https://pko-bonus24.com/oferta/4 -->\n<html><head>{PIXEL_A}"
                    f"</head><body onload=alert(1)><p>{TEXT_A} {PHONE_A}</p></body></html>", encoding="utf-8")
    out = run("file", html, corpus)
    assert out["assignment"]["decision"] == "assign" and out["input_report"]["files"][0]["status"] == "ok"
    import numpy as np

    img = tmp_path / "kreacja.png"
    Image.fromarray(np.random.default_rng(7).integers(0, 255, (240, 320, 3), dtype=np.uint8)).save(img)
    out = run("file", img, corpus)
    assert out["input_report"]["files"][0]["status"] == "ok" and "pdq" in genes_of(corpus, out["incident_id"])
    z = tmp_path / "incydent.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inc/Strona.html", html.read_text(encoding="utf-8"))
        zf.writestr("inc/kreacja.png", img.read_bytes())
    out = run("file", z, corpus)
    assert out["assignment"]["decision"] == "assign"


def test_media_video_file(corpus: Settings, tmp_path: Path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    v = tmp_path / "reklama.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5",
                    "-t", "2", "-pix_fmt", "yuv420p", str(v)], check=True, timeout=60)
    out = run("file", v, corpus)
    rep = out["input_report"]["files"][0]
    assert rep["role"] in ("screen_recording", "ad_video") and rep["status"] == "ok", rep


# ----------------------------------------------------------------------------------------------- 5. robustness


def test_duplicate_corrupt_empty_unknown_huge(corpus: Settings, tmp_path: Path, make_png):
    img = tmp_path / "a.png"
    img.write_bytes(make_png((1, 2, 3), (200, 150)))
    first = run("file", img, corpus)
    again = tmp_path / "kopia innej nazwy.png"
    again.write_bytes(img.read_bytes())
    dup = run("file", again, corpus)
    assert dup["incident_id"] == first["incident_id"]  # same bytes → same incident, idempotent
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)
    out = run("file", bad, corpus)
    assert out["assignment"]["decision"] == "abstain" and out["input_report"]["files"][0]["status"] == "unreadable"
    unk = tmp_path / "dane.bin"
    unk.write_bytes(bytes(range(256)) * 8)
    out = run("file", unk, corpus)
    assert out["input_report"]["files"][0]["status"] == "unsupported"
    assert out["assignment"]["decision"] == "abstain" and "nie odczytano" in out["assignment"]["reason_pl"]
    empty = tmp_path / "pusty"
    empty.mkdir()
    out = run("file", empty, corpus)
    assert out["assignment"]["decision"] == "abstain" and "pusty folder" in out["input_report"]["summary_pl"]
    folder = tmp_path / "duzy"
    folder.mkdir()
    (folder / "Strona.html").write_text(f"<html><body>{TEXT_A} {PHONE_A}</body></html>", encoding="utf-8")
    (folder / "huge.mp4").write_bytes(b"\x00" * (corpus.max_file_mb * 1024 * 1024 + 10))
    (folder / "zero.txt").write_bytes(b"")
    out = run("file", folder, corpus.model_copy(update={"max_upload_mb": corpus.max_file_mb}))
    st = {f["path"]: f for f in out["input_report"]["files"]}
    assert st["huge.mp4"]["status"] == "skipped" and st["huge.mp4"]["code"] == "FILE_TOO_LARGE"
    assert st["zero.txt"]["code"] == "FILE_EMPTY" and st["Strona.html"]["status"] == "ok"


# ------------------------------------------------------------------------------------------- hostile inputs


def test_zip_traversal_symlink_and_bomb(corpus: Settings, tmp_path: Path):
    z = tmp_path / "trav.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../evil.html", "<html>x</html>")
        zf.writestr("/abs.html", "<html>x</html>")
        info = zipfile.ZipInfo("inc/link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
        zf.writestr("inc/Strona.html", f"<html><body>{TEXT_A}</body></html>")
    out = run("file", z, corpus)
    codes = {f.get("code") for f in out["input_report"]["files"] if f["status"] == "skipped"}
    assert {"ZIP_PATH_TRAVERSAL", "ZIP_SYMLINK"} <= codes, out["input_report"]
    assert not (tmp_path / "evil.html").exists() and not Path("/abs.html").exists()
    bomb = tmp_path / "bomb.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inc/a.html", b"<html>" + b"0" * 30_000_000)
    bomb.write_bytes(buf.getvalue())
    out = run("file", bomb, corpus)
    assert out["assignment"] is None and out["input_report"]["files"][0]["code"] == "ZIP_BOMB_SUSPECTED"


def test_svg_and_html_scripts_are_never_executed_or_served_same_origin(corpus: Settings, tmp_path: Path):
    from fastapi.testclient import TestClient

    from hydra.api import create_app

    svg = tmp_path / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>fetch("//x")</script>'
                   f"<text>{TEXT_A}</text></svg>", encoding="utf-8")
    out = run("file", svg, corpus)
    assert out["input_report"]["files"][0]["status"] == "ok"  # its <text>, read as data
    assert "5000 PLN" in genes_of(corpus, out["incident_id"])["entity_price"]
    with connect(corpus.db_path, readonly=True) as conn:
        aid = conn.execute("SELECT artifact_id FROM incident_artifacts WHERE incident_id=?",
                           (out["incident_id"],)).fetchone()[0]
    with TestClient(create_app(corpus)) as c:
        r = c.get(f"/v1/artifacts/{aid}/content")
        assert r.status_code == 200
        # served in a sandbox (unique origin, no scripts) and never sniffed into another type
        assert r.headers["content-security-policy"] == "sandbox"
        assert r.headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------------------------------------------ API + CLI


def _cli_json(output: str) -> dict:
    """The decision JSON printed by `hydra assign` (log lines of the extractors may surround it)."""
    import re

    m = re.search(r"^\{$.*^\}$", output, re.M | re.S)
    assert m, output
    return json.loads(m.group(0))


def test_api_and_cli_paths(corpus: Settings, tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    import hydra.api_task as api_task
    import hydra.pipeline as pipeline
    from hydra.api import create_app
    from hydra.genome import txt_sources
    from hydra.genome.ocr_pool import OcrPool
    from hydra.ml.ocr import OcrLine

    assert api_task._CORE_KIND["screenshot"] == "screenshot"
    real = pipeline.analyse_input
    monkeypatch.setattr(pipeline, "analyse_input", lambda *a, **k: real(*a, **{**k, "only": ONLY}))
    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    line = OcrLine("https://pko-bonus24.com/oferta/56", (10, 10, 600, 40), 0.95, "fake")
    monkeypatch.setattr(txt_sources, "shared_pool",
                        lambda *a, **k: OcrPool(corpus.cache_dir, engine_fn=lambda im: [line]))
    p = tmp_path / "czat.txt"
    p.write_text(WA, encoding="utf-8")
    shot = _shot(tmp_path, 240).read_bytes()
    with TestClient(create_app(corpus)) as c:
        jobs = [c.post("/v1/analyse", json={"url": "https://pko-bonus24.com/oferta/12"}).json()["job_id"],
                c.post("/v1/analyse", json={"hint": "PKO bonus"}).json()["job_id"],
                c.post("/v1/analyse", files={"files": ("czat.txt", p.read_bytes(), "text/plain")}).json()["job_id"],
                c.post("/v1/analyse", data={"kind": "screenshot"},
                       files={"files": ("zrzut.png", shot, "image/png")}).json()["job_id"]]
        for j in jobs:
            for _ in range(200):
                r = c.get(f"/v1/analyse/{j}").json()
                if r["job"]["status"] in ("done", "failed"):
                    break
                time.sleep(0.05)
            assert r["job"]["status"] == "done", r["job"]
            assert r["assignment"] or r["result"].get("search"), r
            if r["result"].get("kind") == "screenshot":
                assert r["result"]["urls_in_image"][0] == "https://pko-bonus24.com/oferta/56"
            if r["result"].get("kind") in ("file", "url", "screenshot"):
                assert r["result"]["input_report"]["files"], r["result"]
    from typer.testing import CliRunner

    import hydra.config
    from hydra.cli import app

    # get_settings is lru_cached: patch it, never rely on HYDRA_* env here (it would hit the real state)
    monkeypatch.setattr(hydra.config, "get_settings", lambda: corpus)
    only = [x for n in ONLY for x in ("--only", n)]
    res = CliRunner().invoke(app, ["assign", str(p), *only])
    assert res.exit_code == 0, res.output
    cli = _cli_json(res.output)
    assert cli["assignment"]["decision"] == "assign", cli["assignment"]["reason_pl"]
    assert cli["input_report"]["files"][0]["status"] == "ok"
    res = CliRunner().invoke(app, ["assign", "PKO bonus", "--kind", "hint"])
    assert res.exit_code == 0 and _cli_json(res.output)["search"]["incidents"], res.output
    res = CliRunner().invoke(app, ["assign", "https://pko-bonus24.com/oferta/13", *only])
    assert res.exit_code == 0 and "niedostępna offline" in " ".join(_cli_json(res.output)["notes"]), res.output


def test_guard_default_settings_never_point_at_the_real_state(tmp_path: Path):
    """tests/conftest.py `_never_the_default_state`: the cached default Settings live under pytest's temp root."""
    import tempfile

    from hydra.config import get_settings

    s = get_settings()
    real = Path.home() / "HydraNexus" / "state"
    assert s.state_dir != real and not str(s.state_dir).startswith(str(real))
    assert Path(tempfile.gettempdir()).resolve() in Path(s.state_dir).resolve().parents
    assert Path(tempfile.gettempdir()).resolve() in Path(s.cache_dir).resolve().parents


# ------------------------------------------------------------------------- audit B8 / B14 (GAP_AUDIT_2026-11-21)


def test_reupload_under_another_name_is_not_appended(corpus: Settings, tmp_path: Path):
    a = tmp_path / "czat.txt"
    a.write_text(WA, encoding="utf-8")
    first = run("file", a, corpus)
    b = tmp_path / "kopia czatu.txt"
    b.write_bytes(a.read_bytes())
    again = run("file", b, corpus)
    assert again["incident_id"] == first["incident_id"] and again["duplicate_of"] == first["incident_id"]
    with connect(corpus.db_path, readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM incident_artifacts WHERE incident_id=?",
                         (first["incident_id"],)).fetchone()[0]
    assert n == 1 and again["assignment"]["decision"] == first["assignment"]["decision"]


def test_url_without_scheme_and_corrupt_zip(corpus: Settings, tmp_path: Path):
    from hydra.pipeline import detect_input_kind

    assert detect_input_kind("pko-bonus24.com/oferta/5") == "url" and detect_input_kind("czat.txt") == "file"
    out = run("url", "pko-bonus24.com/oferta/5", corpus)
    assert genes_of(corpus, out["incident_id"])["landing_host"] == {"pko-bonus24.com"}
    z = tmp_path / "zepsuty.zip"
    z.write_bytes(b"PK\x03\x04" + b"\x00" * 40 + b"garbage")
    out = run("file", z, corpus)
    assert out["assignment"]["decision"] == "abstain", out["assignment"]["reason_pl"]
    assert out["input_report"]["files"][0]["status"] in ("unsupported", "unreadable")


def _heic(tmp_path: Path) -> Path | None:
    import shutil
    import subprocess

    import numpy as np

    png = tmp_path / "src.png"
    Image.fromarray(np.random.default_rng(3).integers(0, 255, (120, 160, 3), dtype=np.uint8)).save(png)
    out = tmp_path / "zdjecie.heic"
    if shutil.which("sips"):
        subprocess.run(["sips", "-s", "format", "heic", str(png), "--out", str(out)], capture_output=True, timeout=60)
    elif shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(png), "-c:v", "libx265", "-f", "heif", str(out)],
                       capture_output=True, timeout=60)
    return out if out.is_file() and out.stat().st_size > 0 else None


def test_heic_image_gets_a_png_rendition(corpus: Settings, tmp_path: Path):
    from hydra.adapters.nask_folder import heic_to_png

    h = _heic(tmp_path)
    if h is None or heic_to_png(h) is None:
        pytest.skip("no HEIC encoder/decoder here (sips, or ffmpeg >= 7.1 with HEIF)")
    out = run("file", h, corpus)
    files = {f["path"]: f for f in out["input_report"]["files"]}
    assert files["zdjecie.heic.png"]["status"] == "ok" and files["zdjecie.heic"]["role"] == "other", files
    assert "pdq" in genes_of(corpus, out["incident_id"])


def test_dataset_zip_uploaded_through_the_api_runs_as_a_corpus(corpus: Settings, monkeypatch):
    from fastapi.testclient import TestClient

    import hydra.pipeline as pipeline
    from hydra.api import create_app

    real = pipeline.analyse
    monkeypatch.setattr(pipeline, "analyse_corpus",
                        lambda paths, s, writer=None, **k: real(paths, s, writer=writer, **{**k, "only": ONLY}))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in (1, 2, 3):
            zf.writestr(f"ds/{i}/Strona.html", f"<!-- saved from url=(0040)https://zbior{i}.com/x -->\n"
                                              f"<html><body>{TEXT_A} {i}</body></html>")
    with TestClient(create_app(corpus)) as c:
        r = c.post("/v1/analyse", files={"files": ("dataset.zip", buf.getvalue(), "application/zip")}).json()
        assert r["mode"] == "corpus", r
        for _ in range(600):
            res = c.get(f"/v1/analyse/{r['job_id']}").json()
            if res["job"]["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
    assert res["job"]["status"] == "done", res["job"]
    assert res["result"]["kind"] == "corpus" and res["result"]["metrics"]["incidents"] == 3, res["result"]


def test_nested_zip_and_root_chat_are_one_incident(corpus: Settings, tmp_path: Path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("Strona.html", f"<!-- saved from url=(0040)https://pko-bonus24.com/oferta/6 -->\n"
                                   f"<html><head>{PIXEL_A}</head><body>{TEXT_A} {PHONE_A}</body></html>")
    outer = tmp_path / "paczka.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("chat.txt", WA)
        zf.writestr("inner/incident.zip", inner.getvalue())
    out = run("file", outer, corpus)
    paths = {f["path"]: f["status"] for f in out["input_report"]["files"]}
    assert paths.get("chat.txt") == "ok" and paths.get("inner/incident.zip!/Strona.html") == "ok", paths
    assert out["assignment"]["decision"] == "assign", out["assignment"]["reason_pl"]
