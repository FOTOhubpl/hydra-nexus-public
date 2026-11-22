"""Generated capture files for public-API workflow tests; no mocked genes or vectors."""

from __future__ import annotations

import hashlib
import io
import json
import random
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from PIL import Image

REAL_WEB_EXTRACTORS = ["artifact_sha256", "inf_url", "inf_dom", "inf_har", "txt_entities"]


def make_capture(folder: Path, operator: int, variant: str, *, har: bool = True) -> Path:
    folder.mkdir(parents=True)
    host = f"capture-{variant}.example.test"
    pixel = str(700000000000000 + operator)
    landing = str(uuid5(NAMESPACE_URL, f"synthetic-operator-{operator}/landing"))
    offer = str(uuid5(NAMESPACE_URL, f"synthetic-operator-{operator}/offer"))
    phone = f"+48 6{operator:02} 222 333"
    script = f'fbq("init","{pixel}");var landing_uuid="{landing}";'
    text = (
        f"<!-- saved from url=(0060)https://{host}/oferta -->"
        f"<html><head><title>Syntetyczna oferta {variant}</title></head><body>"
        f"<main><h1>Materiał testowy {variant}</h1>"
        f"<p>Kontakt {phone}, operator{operator}@example.test.</p>"
        f'<form method="post" action="https://{host}/api/lead">'
        f'<input name="phone"><input type="hidden" name="offer_uuid" value="{offer}">'
        f'<input type="hidden" name="sub1" value="synthetic-{operator}"></form>'
        f"<script>{script}</script>"
        "<p>Jest to wyłącznie syntetyczny dokument do kontroli przepływu danych. "
        "Nie reprezentuje prawdziwej reklamy, firmy ani osoby.</p></main></body></html>"
    )
    (folder / "Strona.html").write_text(text, encoding="utf-8")
    if har:
        entry = {
            "startedDateTime": "2026-11-21T12:00:00.000Z", "time": 1,
            "request": {"method": "POST", "url": f"https://{host}/api/lead", "httpVersion": "HTTP/1.1",
                        "headers": [], "cookies": [], "queryString": [], "headersSize": -1, "bodySize": -1,
                        "postData": {"mimeType": "application/json",
                                     "text": json.dumps({"offer_uuid": offer, "phone": phone})}},
            "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1",
                         "headers": [{"name": "Content-Type", "value": "application/javascript"}],
                         "cookies": [], "content": {"mimeType": "application/javascript",
                                                    "size": len(script), "text": script},
                         "redirectURL": "", "headersSize": -1, "bodySize": len(script)},
            "serverIPAddress": f"192.0.2.{operator + 1}", "cache": {},
            "timings": {"send": 0, "wait": 1, "receive": 0},
        }
        document = {"log": {"version": "1.2", "creator": {"name": "synthetic-audit", "version": "1"},
                            "entries": [entry]}}
        (folder / "capture.har").write_text(json.dumps(document), encoding="utf-8")
    return folder


def web_corpus(root: Path) -> Path:
    for name, operator in (("a1", 1), ("a2", 1), ("b1", 2), ("b2", 2)):
        make_capture(root / name, operator, name)
    for index in range(8):
        make_capture(root / f"background-{index}", index + 10, f"background-{index}")
    return root


def creative_bytes(seed: int = 20261121) -> bytes:
    rng = random.Random(seed)
    pixels = bytes(rng.randrange(256) for _ in range(64 * 48 * 3))
    image = Image.frombytes("RGB", (64, 48), pixels)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
