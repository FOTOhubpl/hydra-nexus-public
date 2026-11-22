#!/usr/bin/env python3
"""Fetch and parse Polish public warning lists into replayable offline snapshots (PL-SCAM stream, 2026-11-21).

    python3 scripts/fetch_public_warnings.py --out "$HYDRA_DATA_DIR/external" [--sources knf,mf]
        [--from-file knf=page.html]

Writes `<out>/knf/knf_warnings.json` and `<out>/mf_hazard/register.json`, each with `SNAPSHOT.json` (url,
retrieved_at UTC, sha256 and size of the raw response, entries, parser version) and the raw response next to it.
Network access happens only here, only when run explicitly (team rule: on the server); the product reads the
snapshots (`hydra/enrich/public_warnings.py`). Standard library only. `--from-file` parses a saved raw file instead
of downloading (replay / tests).

Sources and terms: KNF Lista ostrzeżeń publicznych (https://www.knf.gov.pl/dla_konsumenta/ostrzezenia_publiczne),
MF Rejestr domen służących do oferowania gier hazardowych niezgodnie z ustawą (https://hazard.mf.gov.pl/api/Register);
both are public-sector registers. Entries may name natural persons: snapshots never go into git.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PARSER_VERSION = 2
UA = "Mozilla/5.0 (compatible; public-warning-snapshot/1; research)"
URLS = {
    "knf": "https://www.knf.gov.pl/dla_konsumenta/ostrzezenia_publiczne",
    "mf": "https://hazard.mf.gov.pl/api/Register",
}
# Host-like tokens inside KNF entry text: "www.x.pl", "https://x.com/", "x.exchange", "apexfinbgw.sbs".
_HOST = re.compile(r"(?<![@\w.-])(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
                   re.I)
_NOT_TLD = {"sp", "z", "o", "s", "a", "ul", "nr", "pl.", "art", "ust", "tj", "np", "m", "in", "dz", "u"}
_FILE_EXT = {"pdf", "html", "htm", "php", "jpg", "png", "doc", "docx"}


def fetch(url: str) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pl"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — fixed public URLs
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def _cell_text(c: str) -> str:
    c = re.sub(r"<br\s*/?>", "\n", c, flags=re.I)
    c = re.sub(r"<[^>]+>", "", c)
    return html.unescape(c).replace("\xa0", " ").strip()


def hosts_in(text: str) -> list[str]:
    out = []
    for m in _HOST.finditer(text):
        h = m.group(1).lower().rstrip(".")
        labels = h.split(".")
        tld = labels[-1]
        if len(labels) < 2 or tld in _NOT_TLD or tld in _FILE_EXT or any(not x for x in labels):
            continue
        if re.fullmatch(r"[a-z]\.[a-z]{1,3}", h) or re.fullmatch(r"(sp|s|o)\.[a-z]+", h):
            continue  # "z o.o", "S.A." fragments
        if h.startswith("www."):
            h = h[4:]
        if "." not in h or re.search(r"(^|\.)gov(\.[a-z]{2})?$|(^|\.)europa\.eu$|(^|\.)knf\.gov\.pl$", h):
            continue  # "www.fidjusbank" without a TLD; registers cited as references (companieshouse.gov.uk)
        if h not in out:
            out.append(h)
    return out


def parse_knf(raw: bytes) -> list[dict]:
    """One record per table row: table index, category heading, lp, name, ids, prosecutor, rulings, info, domains."""
    x = raw.decode("utf-8", "replace")
    out: list[dict] = []
    tables = list(re.finditer(r"<table\b.*?</table>", x, re.S))
    prev_end = 0
    for ti, tm in enumerate(tables):
        before = x[prev_end:tm.start()]
        prev_end = tm.end()
        heads = [_cell_text(h) for h in re.findall(r"<(?:h[1-6]|p|div|span|button)[^>]*>([^<]{20,600})<", before)]
        cands = [h for h in heads if re.search(r"Zawiadomieni|Postępowani|art\. ?\d+", h)]
        category = (cands[-1] if cands else (heads[-1] if heads else ""))[:400]
        for row in re.findall(r"<tr>(.*?)</tr>", tm.group(0), re.S):
            cells = [_cell_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) < 2:
                continue
            lp = re.sub(r"\D", "", cells[0]) or None
            name = cells[1]
            info = " ".join(cells[2:])
            out.append({"table": ti, "category": category, "lp": int(lp) if lp else None, "name": name,
                        "ids": cells[2] if len(cells) > 2 else "", "prosecutor": cells[3] if len(cells) > 3 else "",
                        "rulings": cells[4] if len(cells) > 4 else "", "info": cells[5] if len(cells) > 5 else "",
                        "domains": hosts_in(name + " " + info)})
    return out


def parse_mf(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    out = []
    for el in root:
        if not el.tag.endswith("PozycjaRejestru"):
            continue
        rec = {"lp": int(el.get("Lp")) if (el.get("Lp") or "").isdigit() else el.get("Lp")}
        for c in el:
            tag = c.tag.split("}", 1)[-1]
            if tag == "AdresDomeny":
                rec["domain"] = (c.text or "").strip().lower()
            elif tag == "DataWpisu":
                rec["date_added"] = (c.text or "").strip()
            elif tag == "DataWykreslenia":
                rec["date_removed"] = (c.text or "").strip() or None
        if rec.get("domain"):
            out.append(rec)
    return out


def write(out_dir: Path, name: str, raw_name: str, raw: bytes, records: list[dict], url: str, headers: dict,
          retrieved_at: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / raw_name).write_bytes(raw)
    (out_dir / name).write_text(json.dumps(records, ensure_ascii=False, indent=0), encoding="utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    prev = out_dir / "SNAPSHOT.json"
    if prev.is_file():  # re-parse of the same bytes keeps the original retrieval time
        old = json.loads(prev.read_text())
        if old.get("sha256") == sha and old.get("retrieved_at"):
            retrieved_at = old["retrieved_at"]
    snap = f"{Path(raw_name).stem}_{retrieved_at.replace(':', '').replace('-', '')[:15]}Z"
    meta = {"url": url, "retrieved_at": retrieved_at, "snapshot": snap, "sha256": sha,
            "size": len(raw), "entries": len(records), "parser_version": PARSER_VERSION,
            "http_last_modified": headers.get("last-modified"), "http_etag": headers.get("etag")}
    if name == "knf_warnings.json":
        meta["entries_with_domains"] = sum(1 for r in records if r["domains"])
        meta["domains"] = len({d for r in records for d in r["domains"]})
    (out_dir / "SNAPSHOT.json").write_text(json.dumps(meta, indent=1))
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sources", default="knf,mf")
    ap.add_argument("--from-file", action="append", default=[], help="name=path of a saved raw response")
    a = ap.parse_args(argv)
    saved = dict(x.split("=", 1) for x in a.from_file)
    for src in [s.strip() for s in a.sources.split(",") if s.strip()]:
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        if src in saved:
            raw, headers = Path(saved[src]).read_bytes(), {}
        else:
            raw, headers = fetch(URLS[src])
            time.sleep(1.0)
        if src == "knf":
            meta = write(a.out / "knf", "knf_warnings.json", "ostrzezenia_publiczne.html", raw, parse_knf(raw),
                         URLS[src], headers, now)
        elif src == "mf":
            meta = write(a.out / "mf_hazard", "register.json", "register.xml", raw, parse_mf(raw), URLS[src],
                         headers, now)
        else:
            print(f"unknown source {src}", file=sys.stderr)
            return 2
        print(src, json.dumps(meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
