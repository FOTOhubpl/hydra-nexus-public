"""Local demonstration receiver for KONTRA evidence packages (simulates an incident-handling system).

Loopback only (`scripts/kontra_receiver.py` refuses any other bind address). It validates the package schema and
its content hash, stores one record per `package_id` (the same package twice → one record, the second answer has
`duplicate: true` and the same receipt), and returns an acknowledgement with a SYNTHETIC case number. It never sends
anything anywhere. Storage: one JSON file per package under the receiver's own directory.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from hydra.events import canonical_json, utcnow
from hydra.export_safe import safe_filename

NOTE_PL = ("Lokalny odbiornik demonstracyjny (loopback): potwierdzenie transportu, numer sprawy syntetyczny."
           " To nie jest zgłoszenie do platformy ani system CTI.")
VOLATILE = {"package_id", "content_sha256", "created_at"}


def create_app(store_dir: Path) -> FastAPI:
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    app = FastAPI(title="KONTRA local receiver (demo)", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/receive")
    async def receive(request: Request) -> dict[str, Any]:
        try:
            pkg = json.loads(await request.body())
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(pkg, dict) or pkg.get("schema_version") != "hydra.kontra.package.v1":
            raise HTTPException(422, "unsupported package schema")
        pid = str(pkg.get("package_id") or "")
        body = {k: v for k, v in pkg.items() if k not in VOLATILE}
        digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()
        if digest != pkg.get("content_sha256") or pid != "kp-" + digest[:24]:
            raise HTTPException(422, "content hash does not match package_id / content_sha256")
        f = store_dir / f"{safe_filename(pid)}.json"
        with lock:
            if f.is_file():
                rec = json.loads(f.read_text(encoding="utf-8"))
                return {**rec["ack"], "duplicate": True}
            ack = {"receipt_id": "rcpt-" + digest[:16], "package_id": pid, "received_at": utcnow(),
                   "duplicate": False, "synthetic_case_id": "SYN-CASE-" + digest[:8].upper(), "note": NOTE_PL}
            f.write_text(json.dumps({"ack": ack, "package": pkg}, ensure_ascii=False), encoding="utf-8")
        return ack

    @app.get("/receipts")
    def receipts() -> dict[str, Any]:
        items = [json.loads(p.read_text(encoding="utf-8"))["ack"] for p in sorted(store_dir.glob("kp-*.json"))]
        return {"count": len(items), "items": items}

    return app
