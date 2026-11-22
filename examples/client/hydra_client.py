#!/usr/bin/env python3
"""HYDRA NEXUS reference client for the integration contract (docs/INTEGRATION_CONTRACT.md). Standard library only.

    submit material (Bearer token, Idempotency-Key)  ->  job id  ->  progress  ->  versioned result + evidence
    ->  Markdown report  ->  Ed25519-signed package  ->  verification against a PINNED key fingerprint
    ->  webhook delivery to a loopback receiver that checks the HMAC signature

Usage (see examples/client/README.md):

    export HYDRA_URL=http://127.0.0.1:<port>  HYDRA_TOKEN=hnx_...
    python examples/client/hydra_client.py run path/to/ad.png --out /tmp/hydra-out
    python examples/client/hydra_client.py receiver --port 0 --secret <16+ chars>     # loopback webhook receiver
    python examples/client/hydra_client.py roundtrip                                 # CSV/Cypher re-import check

Retries: GET requests, and POST requests that carry an Idempotency-Key, are retried on timeouts, connection errors,
429 and 5xx with exponential backoff. A retried submit with the same key never creates a second job.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
RETRY_STATUSES = {429, 500, 502, 503, 504}
SIGNATURE_TOLERANCE_S = 300


class HydraError(RuntimeError):
    """A non-2xx answer: `status`, machine-readable `code` (detail.code) and the decoded body."""

    def __init__(self, status: int, body: Any) -> None:
        detail = body.get("detail") if isinstance(body, dict) else None
        self.status = status
        self.body = body
        self.code = detail.get("code") if isinstance(detail, dict) else None
        msg = detail.get("message") if isinstance(detail, dict) else detail
        super().__init__(f"HTTP {status} {self.code or ''}: {msg if msg is not None else body}")


class HydraClient:
    def __init__(self, base_url: str, token: str | None, *, timeout: float = 30.0, retries: int = 4,
                 backoff_s: float = 0.5) -> None:
        if not urllib.parse.urlsplit(base_url).scheme.startswith("http"):
            raise ValueError("base_url must be http(s)")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff_s = backoff_s
        # No proxies: the API is loopback-only and the token must not pass through an HTTP(S)_PROXY.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------ transport
    def request(self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None,
                retry: bool | None = None) -> tuple[int, dict[str, str], bytes]:
        hdrs = {"Accept": "application/json", **(headers or {})}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        safe = method in ("GET", "HEAD") or "Idempotency-Key" in hdrs
        retry = safe if retry is None else retry
        attempts = self.retries + 1 if retry else 1
        last: Exception | None = None
        for i in range(attempts):
            if i:
                time.sleep(self.backoff_s * 2 ** (i - 1))
            req = urllib.request.Request(self.base_url + path, data=body, headers=hdrs, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
            except urllib.error.HTTPError as exc:
                data = exc.read()
                if exc.code in RETRY_STATUSES and i + 1 < attempts:
                    last = exc
                    continue
                try:
                    parsed: Any = json.loads(data)
                except ValueError:
                    parsed = data.decode("utf-8", "replace")
                raise HydraError(exc.code, parsed) from None
            except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
                last = exc
                if i + 1 < attempts:
                    continue
                raise
        raise RuntimeError(f"unreachable: {last}")

    def json(self, method: str, path: str, payload: Any = None, headers: dict[str, str] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode()
        h = {**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
        return json.loads(self.request(method, path, body=body, headers=h)[2])

    # ------------------------------------------------------------------ submit
    def submit_files(self, files: list[Path], *, idempotency_key: str | None = None) -> dict[str, Any]:
        boundary = "hydra-" + uuid.uuid4().hex
        buf = io.BytesIO()
        for f in files:
            name = Path(f).name.replace('"', "_")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
                      f"Content-Type: {ctype}\r\n\r\n".encode())
            buf.write(Path(f).read_bytes())
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Idempotency-Key": idempotency_key or new_idempotency_key()}
        return json.loads(self.request("POST", "/v1/analyse", body=buf.getvalue(), headers=headers)[2])

    def submit_json(self, payload: dict[str, str], *, idempotency_key: str | None = None) -> dict[str, Any]:
        """`{"url": ...}`, `{"hint": ...}` or `{"path": ...}` (a path the server is allowed to read)."""
        return self.json("POST", "/v1/analyse", payload,
                         headers={"Idempotency-Key": idempotency_key or new_idempotency_key()})

    # ------------------------------------------------------------------ read
    def progress(self, job_id: str) -> dict[str, Any]:
        return self.json("GET", f"/v1/analyse/{job_id}/progress")

    def result(self, job_id: str) -> dict[str, Any]:
        body = self.json("GET", f"/v1/jobs/{job_id}/result?schema_version={SCHEMA_VERSION}")
        if body.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(f"unexpected schema_version {body.get('schema_version')!r}")
        return body

    def wait(self, job_id: str, *, timeout_s: float = 900.0, poll_s: float = 1.0, on_progress=None) -> dict[str, Any]:
        t_end = time.monotonic() + timeout_s
        while True:
            body = self.result(job_id)
            if body["terminal"]:
                return body
            if on_progress is not None:
                try:
                    on_progress(self.progress(job_id))
                except HydraError:
                    pass
            if time.monotonic() > t_end:
                raise TimeoutError(f"job {job_id} not finished after {timeout_s}s (status {body['status']})")
            time.sleep(poll_s)

    def report(self, job_id: str) -> str:
        return self.request("GET", f"/v1/jobs/{job_id}/report", headers={"Accept": "text/markdown"})[2].decode()

    def package(self, job_id: str) -> tuple[bytes, str]:
        """(ZIP bytes, fingerprint the server says it signed with). Never trust that fingerprint: pin your own."""
        _s, headers, data = self.request("GET", f"/v1/jobs/{job_id}/package", headers={"Accept": "application/zip"})
        return data, headers.get("x-hydra-signing-key", "")

    def signing_key(self) -> str:
        return self.json("GET", "/v1/exports/signing-key")["fingerprint"]

    def verify(self, package: bytes, pinned_fingerprint: str) -> dict[str, Any]:
        if not pinned_fingerprint:
            raise ValueError("verification needs a pinned key fingerprint (obtained out of band)")
        q = urllib.parse.urlencode({"fingerprint": pinned_fingerprint})
        _s, _h, data = self.request("POST", f"/v1/exports/verify?{q}", body=package,
                                    headers={"Content-Type": "application/zip"}, retry=True)
        return json.loads(data)

    # ------------------------------------------------------------------ webhooks (admin token)
    def add_webhook(self, url: str, secret: str, events: list[str] | None = None, name: str = "client") -> dict:
        return self.json("POST", "/v1/integrations/webhooks",
                         {"name": name, "url": url, "events": events or ["job"], "enabled": True, "secret": secret})

    def deliveries(self, endpoint_id: str) -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({"endpoint_id": endpoint_id})
        return self.json("GET", f"/v1/integrations/webhooks/deliveries?{q}")["deliveries"]

    def retry_delivery(self, delivery_id: str) -> dict[str, Any]:
        return self.json("POST", f"/v1/integrations/webhooks/deliveries/{delivery_id}/retry", {})

    # ------------------------------------------------------------------ graph export round trip
    def neo4j_roundtrip(self, run_id: str | None = None) -> dict[str, Any]:
        q = f"&run_id={urllib.parse.quote(run_id)}" if run_id else ""
        zdata = self.request("GET", f"/v1/export/neo4j?format=zip{q}", headers={"Accept": "*/*"})[2]
        nodes = self.request("GET", f"/v1/export/neo4j?format=nodes_csv{q}", headers={"Accept": "*/*"})[2]
        edges = self.request("GET", f"/v1/export/neo4j?format=edges_csv{q}", headers={"Accept": "*/*"})[2]
        cypher = self.request("GET", f"/v1/export/neo4j?format=cypher{q}", headers={"Accept": "*/*"})[2]
        return roundtrip_check(zdata, nodes.decode("utf-8"), edges.decode("utf-8"), cypher.decode("utf-8"))


def new_idempotency_key() -> str:
    return "cli-" + uuid.uuid4().hex


# ----------------------------------------------------------------------------- package inspection (offline)


def package_manifest(package: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        name = next(n for n in zf.namelist() if n.endswith("SIGNED_MANIFEST.json"))
        return json.loads(zf.read(name))


def offline_hash_check(package: bytes) -> list[str]:
    """Hash/size check of every file against the manifest (no signature check: stdlib has no Ed25519; the
    signature is checked by `verify()` or `hydra verify-export`)."""
    errors: list[str] = []
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        mname = next(n for n in zf.namelist() if n.endswith("SIGNED_MANIFEST.json"))
        prefix = mname[: -len("SIGNED_MANIFEST.json")]
        manifest = json.loads(zf.read(mname))
        listed = {f["path"]: f for f in manifest["files"]}
        present = {n[len(prefix):] for n in zf.namelist() if not n.endswith("/")} - {
            "SIGNED_MANIFEST.json", "SIGNED_MANIFEST.sig"}
        errors += [f"missing: {p}" for p in sorted(set(listed) - present)]
        errors += [f"extra: {p}" for p in sorted(present - set(listed))]
        for p in sorted(set(listed) & present):
            data = zf.read(prefix + p)
            if hashlib.sha256(data).hexdigest() != listed[p]["sha256"] or len(data) != listed[p]["size"]:
                errors.append(f"sha256/size mismatch: {p}")
    return errors


# ----------------------------------------------------------------------------- CSV / Cypher round trip

_UNWIND_ROW = re.compile(r"^  \{")
_MERGE_NODE = re.compile(r"^MERGE \(n:(\w+) ")
_MERGE_REL = re.compile(r"^MERGE \(s\)-\[x:(\w+)\]->\(e\)")


def roundtrip_check(zip_bytes: bytes, nodes_csv: str, edges_csv: str, cypher: str) -> dict[str, Any]:
    """Re-import the exported graph into a temporary SQLite database and compare counts across renderings.

    1. per-table CSV files of the ZIP (LOAD CSV folder) -> temp tables `node(label, key)`, `edge(type, src, dst)`;
       every edge endpoint must resolve to an imported node (foreign keys enforced);
    2. `neo4j-admin` nodes/edges CSV -> counts per label / relationship type;
    3. self-contained `graph.cypher` -> rows of every `UNWIND [...]` statement per label / relationship type.
    All three must give identical node and edge counts."""
    labels = {"incidents": ("Incident", "id"), "campaigns": ("Campaign", "id"), "groups": ("CampaignGroup", "id"),
              "genes": ("Gene", "key")}
    rels = {"member_of": ("MEMBER_OF", "Incident", "incident_id", "Campaign", "campaign_id"),
            "in_group": ("IN_GROUP", "Campaign", "campaign_id", "CampaignGroup", "group_id"),
            "has_gene": ("HAS_GENE", "Incident", "incident_id", "Gene", "gene_key"),
            "linked_to": ("LINKED_TO", "Incident", "a", "Incident", "b")}
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE node (nid TEXT PRIMARY KEY, label TEXT NOT NULL)")
    db.execute("CREATE TABLE edge (type TEXT NOT NULL, src TEXT NOT NULL REFERENCES node(nid),"
               " dst TEXT NOT NULL REFERENCES node(nid))")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        files = {Path(n).stem: n for n in zf.namelist() if n.endswith(".csv")}

        def rows(stem: str) -> list[dict[str, str]]:
            return list(csv.DictReader(io.StringIO(zf.read(files[stem]).decode("utf-8"))))

        for stem, (label, key) in labels.items():
            db.executemany("INSERT INTO node VALUES (?, ?)", [(f"{label}:{r[key]}", label) for r in rows(stem)])
        for stem, (typ, sl, scol, el, ecol) in rels.items():
            db.executemany("INSERT INTO edge VALUES (?, ?, ?)",
                           [(typ, f"{sl}:{r[scol]}", f"{el}:{r[ecol]}") for r in rows(stem)])
    zip_nodes = dict(db.execute("SELECT label, COUNT(*) FROM node GROUP BY 1").fetchall())
    zip_edges = dict(db.execute("SELECT type, COUNT(*) FROM edge GROUP BY 1").fetchall())
    dangling = db.execute("SELECT COUNT(*) FROM edge WHERE src NOT IN (SELECT nid FROM node)"
                          " OR dst NOT IN (SELECT nid FROM node)").fetchone()[0]
    admin_nodes = Counter(r[":LABEL"] for r in csv.DictReader(io.StringIO(nodes_csv)))
    admin_edges = Counter(r[":TYPE"] for r in csv.DictReader(io.StringIO(edges_csv)))
    cy_nodes: Counter[str] = Counter()
    cy_edges: Counter[str] = Counter()
    pending = 0
    for line in cypher.splitlines():
        if _UNWIND_ROW.match(line):
            pending += 1
        elif m := _MERGE_NODE.match(line):
            cy_nodes[m.group(1)] += pending
            pending = 0
        elif m := _MERGE_REL.match(line):
            cy_edges[m.group(1)] += pending
            pending = 0

    def norm(c: dict[str, int]) -> dict[str, int]:
        return {k: v for k, v in sorted(c.items()) if v}

    out = {"zip_csv": {"nodes": norm(zip_nodes), "edges": norm(zip_edges)},
           "admin_csv": {"nodes": norm(admin_nodes), "edges": norm(admin_edges)},
           "cypher": {"nodes": norm(cy_nodes), "edges": norm(cy_edges)},
           "dangling_edges": dangling}
    views = [out["zip_csv"], out["admin_csv"], out["cypher"]]
    out["equal"] = dangling == 0 and all(v == views[0] for v in views)
    out["node_total"] = sum(zip_nodes.values())
    out["edge_total"] = sum(zip_edges.values())
    return out


# ----------------------------------------------------------------------------- loopback webhook receiver


def verify_signature(secret: str, headers: dict[str, str], body: bytes, now: float | None = None) -> bool:
    """X-Hydra-Signature = sha256=<hex HMAC-SHA256(secret, f"{X-Hydra-Timestamp}.{body}")>, |now - ts| <= 300 s."""
    ts = headers.get("x-hydra-timestamp", "")
    sig = headers.get("x-hydra-signature", "")
    if not ts.isdigit() or not sig.startswith("sha256="):
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > SIGNATURE_TOLERANCE_S:
        return False
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig[len("sha256="):])


class Receiver:
    """Controlled loopback receiver. `script` = list of HTTP statuses to answer in order (then 200), e.g. [503, 503]
    to exercise retries or [400] for an immediate dead letter. A request with a bad signature gets 401."""

    def __init__(self, secret: str, port: int = 0, script: list[int] | None = None) -> None:
        self.secret = secret
        self.script = list(script or [])
        self.receipts: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 1 << 20))
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                ok = verify_signature(outer.secret, hdrs, body)
                with outer.lock:
                    status = 401 if not ok else (outer.script.pop(0) if outer.script else 200)
                    outer.receipts.append({"delivery": hdrs.get("x-hydra-delivery"), "event": hdrs.get(
                        "x-hydra-event"), "signature_ok": ok, "status": status,
                        "body": json.loads(body) if ok else None})
                out = json.dumps({"received": ok}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # loopback only, never 0.0.0.0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/hook"

    def __enter__(self) -> Receiver:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


# ----------------------------------------------------------------------------- CLI


def walk_through(c: HydraClient, files: list[Path], out: Path, pinned: str | None = None,
                 log=print) -> dict[str, Any]:  # noqa: ANN001
    out.mkdir(parents=True, exist_ok=True)
    key = new_idempotency_key()
    sub = c.submit_files(files, idempotency_key=key)
    log(f"1. submitted: job {sub['job_id']} (mode {sub.get('mode')}, Idempotency-Key {key})")
    again = c.submit_files(files, idempotency_key=key)
    log(f"2. resubmitted with the same key: job {again['job_id']} replayed={again.get('replayed', False)}")
    res = c.wait(sub["job_id"],
                 on_progress=lambda p: log(f"   progress {p.get('overall_percent')}% {p.get('stage', '')}"))
    (out / "result.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    r = res.get("result") or {}
    log(f"3. result ({res['schema']} v{res['schema_version']}): {res['status']}, decision {r.get('decision')},"
        f" campaign {r.get('campaign_id')}, genes {(res.get('evidence') or {}).get('genes_total')}")
    if res["status"] != "done":
        return {"result": res}
    (out / "report.md").write_text(c.report(sub["job_id"]), encoding="utf-8")
    log(f"4. report: {out / 'report.md'}")
    pkg, claimed = c.package(sub["job_id"])
    (out / "package.zip").write_bytes(pkg)
    pinned = pinned or c.signing_key()  # first use: pin it; later runs pass --pin
    log(f"5. package: {len(pkg)} bytes, signed by {claimed}; pinned {pinned}")
    v = c.verify(pkg, pinned)
    log(f"6. verified: ok={v['ok']} files={v['files_checked']} errors={v['errors']}")
    local = offline_hash_check(pkg)
    log(f"   offline hash check: {'ok' if not local else local}")
    return {"submit": sub, "replay": again, "result": res, "verify": v, "offline_errors": local, "pinned": pinned}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("HYDRA_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--token", default=os.environ.get("HYDRA_TOKEN"))
    ap.add_argument("--timeout", type=float, default=30.0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="submit files and walk the whole contract")
    r.add_argument("files", nargs="+", type=Path)
    r.add_argument("--out", type=Path, default=Path("hydra-client-out"))
    r.add_argument("--pin", help="expected signing key fingerprint (SHA256:...)")
    sub.add_parser("roundtrip", help="CSV/Cypher export re-import count check")
    rc = sub.add_parser("receiver", help="loopback webhook receiver that verifies the HMAC signature")
    rc.add_argument("--port", type=int, default=0)
    rc.add_argument("--secret", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "receiver":
        with Receiver(a.secret, a.port) as rec:
            print(f"listening on {rec.url} (Ctrl+C to stop)", flush=True)
            seen = 0
            try:
                while True:
                    time.sleep(0.5)
                    for x in rec.receipts[seen:]:
                        print(json.dumps(x, ensure_ascii=False), flush=True)
                    seen = len(rec.receipts)
            except KeyboardInterrupt:
                return 0
    c = HydraClient(a.url, a.token, timeout=a.timeout)
    if a.cmd == "roundtrip":
        res = c.neo4j_roundtrip()
        print(json.dumps(res, indent=2))
        return 0 if res["equal"] else 1
    res = walk_through(c, a.files, a.out, a.pin)
    return 0 if (res.get("verify") or {}).get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
