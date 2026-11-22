"""Measure every input kind end to end on a synthetic corpus (no organiser data): works?, genes, decision, seconds.

    uv run python scripts/measure_inputs.py --root ~/HydraNexus/inputs/measure [--live]

Builds the synthetic corpus of tests/inputs (campaign A on pko-bonus24.com, campaign B, 60 fillers) in its own
state under --root, runs `analyse` with every registered extractor (real model weights from HYDRA_MODELS_DIR), then
one `analyse_input` per input kind. `--live` sets HYDRA_ENRICH=1 / HYDRA_OFFLINE=0 for the run (isolated page
fetch and RDAP/DNS/crt.sh/ASN lookups, all cached under --root/cache/enrich for replay); the URL inputs then point at
public pages that are safe to fetch (example.com, the IANA example domain). Prints a Markdown table and writes
--root/measure.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from pathlib import Path


def build_inputs(d: Path) -> list[tuple[str, str, object]]:
    from PIL import Image, ImageDraw, ImageFont

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.inputs.test_inputs_e2e import IBAN_A, PHONE_A, TEXT_A, WA, _pdf

    d.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (1000, 220), "white")
    dr = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=36)
    dr.text((20, 30), "Zarabiaj: https://pko-bonus24.com/oferta/77", fill="black", font=font)
    dr.text((20, 120), f"Zadzwon {PHONE_A}", fill="black", font=font)
    im.save(d / "zrzut.png")
    (d / "WhatsApp Chat.txt").write_text(WA, encoding="utf-8")
    (d / "result.json").write_text(json.dumps({"name": "Inwestycje", "messages": [
        {"id": 1, "type": "message", "date": "2026-11-20T10:01:00", "from": "Anna Inwestor", "text": TEXT_A},
        {"id": 2, "type": "message", "date": "2026-11-20T10:02:00", "from": "Anna Inwestor",
         "text": [{"type": "link", "text": "https://pko-bonus24.com/oferta/8"}, f" tel {PHONE_A}"]}]}),
        encoding="utf-8")
    (d / "reklama.pdf").write_bytes(_pdf(f"Inwestuj z PKO, zadzwon {PHONE_A}", "https://pko-bonus24.com/oferta/3"))
    (d / "ad.html").write_text(f"<!-- saved from url=(0040)https://pko-bonus24.com/oferta/4 -->\n<html><body><p>"
                               f"{TEXT_A} {PHONE_A} {IBAN_A}</p></body></html>", encoding="utf-8")
    with zipfile.ZipFile(d / "incydent.zip", "w") as zf:
        zf.writestr("inc/Strona.html", (d / "ad.html").read_text(encoding="utf-8"))
    (d / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 20)
    (d / "pusty").mkdir(exist_ok=True)
    import subprocess

    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
                    "-t", "3", "-pix_fmt", "yuv420p", str(d / "reklama.mp4")], check=False, timeout=60)
    return [
        ("url (known campaign domain)", "url", "https://pko-bonus24.com/oferta/999?utm_source=fb"),
        ("url (homoglyph)", "url", "https://pkо-bonus24.com/oferta/1"),
        ("url (public page, fetchable)", "url", "https://example.com/"),
        ("screenshot with link (OCR)", "screenshot", d / "zrzut.png"),
        ("hint: product name", "hint", "PKO bonus"),
        ("chat log WhatsApp .txt", "file", d / "WhatsApp Chat.txt"),
        ("chat log Telegram .json", "file", d / "result.json"),
        ("ad file PDF", "file", d / "reklama.pdf"),
        ("ad file HTML", "file", d / "ad.html"),
        ("incident ZIP", "file", d / "incydent.zip"),
        ("media file MP4", "file", d / "reklama.mp4"),
        ("corrupt image", "file", d / "broken.png"),
        ("empty folder", "file", d / "pusty"),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--tag", help="state/output name (default live|offline; e.g. 'replay' for an offline run after"
                    " a live one, which then reads the enrichment and fetch cache)")
    args = ap.parse_args()
    os.environ["HYDRA_ENRICH"] = "1" if args.live else "0"
    os.environ["HYDRA_OFFLINE"] = "0" if args.live else "1"
    from hydra.config import Settings
    from hydra.db import connect
    from hydra.pipeline import analyse, analyse_input

    root = args.root.expanduser()
    tag = args.tag or ("live" if args.live else "offline")
    s = Settings(root=root, data_dir=root / "data", cache_dir=root / "cache", state_dir=root / f"state-{tag}",
                 enrich=args.live, offline=not args.live)
    s.ensure_dirs()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.inputs.test_inputs_e2e import IBAN_A, PHONE_A, TEXT_A, page

    ds = root / "ds"
    if not ds.exists():
        for i in (1, 2, 3):
            page(ds / str(i), f"https://pko-bonus24.com/oferta/{100 + i}?utm_source=fb",
                 f"{TEXT_A} {PHONE_A}. Przelew na {IBAN_A}.", "Inwestycja PKO Bonus")
        for i in (4, 5):
            page(ds / str(i), f"https://tani-buty24.pl/sklep/{i}", "Wyprzedaż butów -90%. Zadzwoń +48 600 200 300",
                 "Buty wyprzedaż")
        for i in range(6, 66):
            page(ds / str(i), f"https://filler{i}-site.com/p/{i}", f"Zupełnie inna treść numer {i} o ogrodzie",
                 f"Ogród {i}")
    t0 = time.perf_counter()
    analyse([ds], s)
    corpus_s = time.perf_counter() - t0
    rows = []
    for label, kind, payload in build_inputs(root / "inputs"):
        t = time.perf_counter()
        try:
            out = analyse_input(kind, payload, s)
            err = None
        except Exception as exc:  # noqa: BLE001 — measured, reported
            out, err = {}, f"{type(exc).__name__}: {exc}"[:200]
        sec = time.perf_counter() - t
        genes, fams = 0, set()
        if out.get("incident_id"):
            with connect(s.db_path, readonly=True) as conn:
                for r in conn.execute("SELECT family, COUNT(*) n FROM genes WHERE incident_id=? AND status='observed'"
                                      " GROUP BY family", (out["incident_id"],)):
                    genes += r["n"]
                    fams.add(r["family"])
        a = out.get("assignment") or {}
        decision = a.get("decision") or ("search: " + str(len(out["search"]["incidents"])) + " hits"
                                         if out.get("search") else "-")
        rows.append({"input": label, "kind": kind, "ok": err is None, "error": err, "genes": genes,
                     "families": sorted(fams), "decision": decision, "campaign": a.get("campaign_id"),
                     "reason_pl": (a.get("reason_pl") or "")[:160], "fetch": (out.get("fetch") or {}).get("status"),
                     "fetch_pl": (out.get("fetch") or {}).get("status_pl"),
                     "report": (out.get("input_report") or {}).get("summary_pl"), "seconds": round(sec, 2)})
    with connect(s.db_path, readonly=True) as conn:
        enr = {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) n FROM enrichment GROUP BY source")}
    res = {"mode": tag, "corpus_seconds": round(corpus_s, 1), "rows": rows, "enrichment_records": enr}
    (root / f"measure-{tag}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"mode={tag} corpus analyse {corpus_s:.1f} s; enrichment records by source: {enr}")
    print("| input | works | genes (families) | decision | fetch | s |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['input']} | {'yes' if r['ok'] else 'NO: ' + str(r['error'])} | {r['genes']}"
              f" ({','.join(r['families'])}) | {r['decision']} | {r['fetch'] or '-'} | {r['seconds']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
