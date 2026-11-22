"""G5-CONTRACT: the external integration contract end to end (docs/INTEGRATION_CONTRACT.md).

One real analysis runs in the module fixture (tests/contract/conftest.py) through the reference client; the tests
below exercise auth, roles, idempotency, result versioning, timeout + retry, webhook retry/dead letter, package
tamper and foreign-key rejection, and the CSV/Cypher export round trip, all over HTTP on 127.0.0.1."""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hydra.events import canonical_json
from hydra.evidence_sign import MANIFEST_NAME, SIG_NAME, fingerprint, public_raw

SECRET = "contract-hmac-secret-0123456789abcdef"
RESULT_KEYS = {"schema", "schema_version", "product_version", "job_id", "kind", "status", "terminal", "progress",
               "created_at", "started_at", "finished_at", "error", "result", "evidence", "links", "disclaimer"}
RESULT_RESULT_KEYS = {"mode", "run_id", "incident_id", "decision", "campaign_id", "group_id", "reason_pl", "model",
                      "candidates", "metrics"}


def _analyse_jobs(live) -> int:
    with live.app.state.hydra.reader() as conn:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='analyse'").fetchone()[0]


def _wait_terminal(c, job_id: str, timeout: float = 120) -> dict:
    return c.wait(job_id, timeout_s=timeout, poll_s=0.2)


# ----------------------------------------------------------------------------------------------- walk-through


def test_walk_through_real_analysis(live):
    w = live.walk
    assert w["replay"]["job_id"] == w["submit"]["job_id"] and w["replay"]["replayed"] is True
    res = w["result"]
    assert res["status"] == "done" and res["result"]["incident_id"] and res["result"]["decision"] in (
        "assign", "new_campaign", "abstain")
    assert res["evidence"]["genes_total"] > 0
    assert w["verify"]["ok"] is True and w["verify"]["key_fingerprint"] == w["pinned"]
    assert w["offline_errors"] == []
    assert (live.root / "out" / "report.md").read_text().startswith("# Raport HYDRA NEXUS")


# ----------------------------------------------------------------------------------------------- auth / roles


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/v1/analyse"), ("GET", "/v1/jobs/x/result"), ("GET", "/v1/jobs/x/package"),
    ("GET", "/v1/jobs/x/report"), ("GET", "/v1/analyse/x/progress"), ("POST", "/v1/exports/verify"),
    ("GET", "/openapi.json")])
def test_auth_required(live, hc, method, path):
    for token in (None, "hnx_not-a-real-token"):
        c = hc.HydraClient(live.base, token, retries=0)
        with pytest.raises(hc.HydraError) as e:
            c.request(method, path, body=b"{}" if method == "POST" else None,
                      headers={"Content-Type": "application/json"})
        assert e.value.status == 401 and e.value.code == "auth_required"


def test_role_enforcement(live, hc):
    viewer, analyst = live.client("viewer", retries=0), live.client("analyst", retries=0)
    with pytest.raises(hc.HydraError) as e:
        viewer.submit_json({"hint": "bonus"})
    assert e.value.status == 403 and e.value.code == "forbidden"
    assert viewer.result(live.job_id)["status"] == "done"  # viewer reads results, reports and packages
    assert viewer.report(live.job_id).startswith("# Raport")
    pkg, _fp = viewer.package(live.job_id)
    assert viewer.verify(pkg, viewer.signing_key())["ok"] is True
    with pytest.raises(hc.HydraError) as e:  # webhook configuration is admin-only
        analyst.add_webhook("http://127.0.0.1:9/hook", SECRET)
    assert e.value.status == 403 and e.value.code == "forbidden"


# ----------------------------------------------------------------------------------------------- idempotency


def test_idempotent_resubmit(live, hc):
    c = live.client("analyst")
    before = _analyse_jobs(live)
    a = c.submit_json({"hint": "kontrakt idempotencja"}, idempotency_key="idem-001")
    b = c.submit_json({"hint": "kontrakt idempotencja"}, idempotency_key="idem-001")
    assert a["job_id"] == b["job_id"] and b["replayed"] is True and "replayed" not in a
    assert _analyse_jobs(live) == before + 1
    # same key, different request -> 409, still no new job
    with pytest.raises(hc.HydraError) as e:
        live.client("analyst", retries=0).submit_json({"hint": "inny materiał"}, idempotency_key="idem-001")
    assert e.value.status == 409 and e.value.code == "idempotency_conflict"
    assert e.value.body["detail"]["job_id"] == a["job_id"]
    # the key is scoped to the user: the admin's idem-001 is a different job
    other = live.client("admin").submit_json({"hint": "kontrakt idempotencja"}, idempotency_key="idem-001")
    assert other["job_id"] != a["job_id"]
    # invalid key -> 400 before anything is stored
    with pytest.raises(hc.HydraError) as e:
        live.client("analyst", retries=0).submit_json({"hint": "x y"}, idempotency_key="bad key with spaces")
    assert e.value.status == 400 and e.value.code == "invalid_idempotency_key"
    # multipart: identical bytes replay, different bytes conflict
    m1 = c.submit_files([live.png], idempotency_key="idem-files")
    assert c.submit_files([live.png], idempotency_key="idem-files")["job_id"] == m1["job_id"]
    other_png = live.root / "other.png"
    other_png.write_bytes(live.png.read_bytes() + b"\0")
    with pytest.raises(hc.HydraError) as e:
        live.client("analyst", retries=0).submit_files([other_png], idempotency_key="idem-files")
    assert e.value.code == "idempotency_conflict"
    for jid in (a["job_id"], other["job_id"], m1["job_id"]):
        _wait_terminal(c, jid)
    with live.app.state.hydra.reader() as conn:
        raw = conn.execute("SELECT params FROM jobs WHERE id=?", (a["job_id"],)).fetchone()[0]
    assert "idem-001" not in raw  # only the scoped hash is stored


def test_client_timeout_then_retry_is_safe(live, hc, monkeypatch):
    """The first POST commits the job but its answer is late (client times out); the client retries with the same
    key; the retry waits for the first and returns the same job id. Exactly one job exists."""
    import hydra.api_task as at

    real = at.create_job

    def slow_create(*a, **kw):
        jid = real(*a, **kw)
        time.sleep(1.5)  # the job row is committed, the HTTP answer is not sent yet
        return jid

    monkeypatch.setattr(at, "create_job", slow_create)
    before = _analyse_jobs(live)
    c = live.client("analyst", timeout=0.5, retries=4, backoff_s=0.2)
    out = c.submit_json({"hint": "kontrakt timeout"}, idempotency_key="idem-timeout")
    monkeypatch.setattr(at, "create_job", real)
    assert out.get("replayed") is True  # the answer the client finally got is the replay
    assert _analyse_jobs(live) == before + 1
    _wait_terminal(live.client("analyst"), out["job_id"])
    # without a key the client does not retry a POST (no duplicate risk)
    monkeypatch.setattr(at, "create_job", slow_create)
    nokey = hc.HydraClient(live.base, live.tokens["analyst"], timeout=0.5, retries=4)
    with pytest.raises(OSError):
        nokey.request("POST", "/v1/analyse", body=json.dumps({"hint": "bez klucza"}).encode(),
                      headers={"Content-Type": "application/json"})
    monkeypatch.setattr(at, "create_job", real)
    time.sleep(1.6)
    assert _analyse_jobs(live) == before + 2  # exactly one job for the single unretried attempt


# ----------------------------------------------------------------------------------------------- versioning


def test_result_schema_versioning(live, hc):
    c = live.client("viewer", retries=0)
    body = c.result(live.job_id)
    assert set(body) == RESULT_KEYS and set(body["result"]) == RESULT_RESULT_KEYS
    assert body["schema"] == "hydra.job_result.v1" and body["schema_version"] == "1"
    _s, headers, _d = c.request("GET", f"/v1/jobs/{live.job_id}/result")
    assert headers["x-hydra-schema"] == "hydra.job_result.v1"
    for q, h in (("?schema_version=2", {}), ("", {"Accept-Version": "2"})):
        with pytest.raises(hc.HydraError) as e:
            c.request("GET", f"/v1/jobs/{live.job_id}/result{q}", headers=h)
        assert e.value.status == 406 and e.value.code == "unsupported_schema_version"
        assert e.value.body["detail"]["supported"] == ["1"]
    with pytest.raises(hc.HydraError) as e:
        c.result("does-not-exist")
    assert e.value.status == 404 and e.value.code == "job_not_found"
    # the published OpenAPI document lists the contract paths
    paths = c.json("GET", "/openapi.json")["paths"]
    for p in ("/v1/analyse", "/v1/jobs/{job_id}/result", "/v1/jobs/{job_id}/report", "/v1/jobs/{job_id}/package",
              "/v1/exports/verify", "/v1/exports/signing-key", "/v1/analyse/{job_id}/progress"):
        assert p in paths, p


def test_report_and_package_need_a_finished_job(live, hc, monkeypatch):
    import hydra.api_task as at

    gate = __import__("threading").Event()
    real = at._pipeline_entry

    def blocked():
        fn = real()

        def run(*a, **kw):
            gate.wait(30)
            return fn(*a, **kw)
        return run

    monkeypatch.setattr(at, "_pipeline_entry", blocked)
    c = live.client("analyst", retries=0)
    jid = c.submit_json({"hint": "kontrakt w toku"})["job_id"]
    try:
        body = c.result(jid)
        assert body["terminal"] is False and body["result"] is None
        for path in (f"/v1/jobs/{jid}/report", f"/v1/jobs/{jid}/package"):
            with pytest.raises(hc.HydraError) as e:
                c.request("GET", path)
            assert e.value.status == 409 and e.value.code == "job_not_finished"
    finally:
        gate.set()
    _wait_terminal(c, jid)


# ----------------------------------------------------------------------------------------------- package


def _rewrite(pkg: bytes, mutate) -> bytes:  # noqa: ANN001
    src = zipfile.ZipFile(io.BytesIO(pkg))
    files = {n: src.read(n) for n in src.namelist()}
    mutate(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    return out.getvalue()


def test_tampered_package_rejected(live, hc):
    c = live.client("viewer")
    pkg, _fp = c.package(live.job_id)
    pinned = c.signing_key()
    assert c.verify(pkg, pinned)["ok"] is True

    def edit_report(files):
        k = next(n for n in files if n.endswith("/report.md"))
        files[k] = files[k].replace(b"new_campaign", b"assign").replace(b"abstain", b"assign") + b"\nzmiana\n"

    def drop_evidence(files):
        del files[next(n for n in files if n.endswith("/evidence.json"))]

    def edit_manifest(files):
        k = next(n for n in files if n.endswith(MANIFEST_NAME))
        m = json.loads(files[k])
        m["run_id"] = "forged-run"
        files[k] = canonical_json(m).encode()

    for mutate, needle in ((edit_report, "sha256 mismatch"), (drop_evidence, "missing file"),
                           (edit_manifest, "signature invalid")):
        bad = _rewrite(pkg, mutate)
        v = c.verify(bad, pinned)
        assert v["ok"] is False and any(needle in e for e in v["errors"]), (needle, v["errors"])
    assert hc.offline_hash_check(_rewrite(pkg, edit_report)) == [
        next(f"sha256/size mismatch: {n.split('/', 1)[1]}" for n in zipfile.ZipFile(io.BytesIO(pkg)).namelist()
             if n.endswith("/report.md"))]


def test_foreign_key_fingerprint_rejected(live, hc):
    """A package re-signed by someone else's key is self-consistent; only the PINNED fingerprint rejects it."""
    c = live.client("viewer")
    pkg, _fp = c.package(live.job_id)
    pinned = c.signing_key()
    foreign = Ed25519PrivateKey.generate()
    fpub = public_raw(foreign.public_key())

    def resign(files):
        k = next(n for n in files if n.endswith(MANIFEST_NAME))
        m = json.loads(files[k])
        m.update(public_key=base64.b64encode(fpub).decode(), key_fingerprint=fingerprint(fpub))
        body = canonical_json(m).encode()
        files[k] = body
        files[k.replace(MANIFEST_NAME, SIG_NAME)] = base64.b64encode(foreign.sign(body)) + b"\n"

    forged = _rewrite(pkg, resign)
    v = c.verify(forged, pinned)
    assert v["ok"] is False and v["key_fingerprint"] == fingerprint(fpub) != pinned
    assert any("key differs from expected" in e for e in v["errors"])
    with pytest.raises(ValueError):
        c.verify(forged, "")  # the client refuses to verify without a pinned key
    # the unpinned server check alone would accept it: this is why the contract requires pinning
    _s, _h, raw = c.request("POST", "/v1/exports/verify", body=forged, headers={"Content-Type": "application/zip"},
                            retry=True)
    assert json.loads(raw)["ok"] is True


# ----------------------------------------------------------------------------------------------- webhooks


def _deliveries_settled(admin, endpoint_id: str, n: int, timeout: float = 20) -> list[dict]:
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        ds = admin.deliveries(endpoint_id)
        if len(ds) >= n and all(d["status"] in ("delivered", "dead") for d in ds):
            return ds
        time.sleep(0.1)
    raise AssertionError(f"deliveries not settled: {admin.deliveries(endpoint_id)}")


def test_webhook_delivery_retry_and_dead_letter(live, hc):
    admin, analyst = live.client("admin"), live.client("analyst")
    # 1) receiver fails twice with 503 -> retried with backoff -> delivered on the third attempt, HMAC verified
    with hc.Receiver(SECRET, script=[503, 503]) as rec:
        ep = admin.add_webhook(rec.url, SECRET, ["job"], name="kontrakt-retry")
        time.sleep(0.5)  # dispatcher cursor starts after existing events
        jid = analyst.submit_json({"hint": "kontrakt webhook"})["job_id"]
        _wait_terminal(analyst, jid)
        ds = _deliveries_settled(admin, ep["id"], 1)
        mine = [d for d in ds if d["status"] == "delivered"]
        assert mine and mine[0]["attempts"] == 3
        ok = [r for r in rec.receipts if r["status"] == 200]
        assert ok and ok[-1]["signature_ok"] and ok[-1]["event"] == "job"
        assert ok[-1]["body"]["event"] == "job" and ok[-1]["body"]["data"]["job_id"] == jid
        assert [r["status"] for r in rec.receipts][:3] == [503, 503, 200]
        admin.json("DELETE", f"/v1/integrations/webhooks/{ep['id']}")
    # 2) receiver answers 400 -> dead letter at once (no retry); a manual retry after the fix delivers it
    with hc.Receiver(SECRET, script=[400]) as rec:
        ep = admin.add_webhook(rec.url, SECRET, ["job"], name="kontrakt-dead")
        time.sleep(0.5)
        jid = analyst.submit_json({"hint": "kontrakt dead letter"})["job_id"]
        _wait_terminal(analyst, jid)
        d = _deliveries_settled(admin, ep["id"], 1)[0]
        assert d["status"] == "dead" and d["attempts"] == 1 and d["last_status_code"] == 400
        again = admin.retry_delivery(d["id"])
        assert again["status"] == "delivered" and rec.receipts[-1]["signature_ok"]
        admin.json("DELETE", f"/v1/integrations/webhooks/{ep['id']}")
    # 3) receiver keeps failing (5xx) -> dead after the maximum number of attempts
    with hc.Receiver(SECRET, script=[503] * 10) as rec:
        ep = admin.add_webhook(rec.url, SECRET, ["job"], name="kontrakt-max")
        time.sleep(0.5)
        jid = analyst.submit_json({"hint": "kontrakt max"})["job_id"]
        _wait_terminal(analyst, jid)
        d = _deliveries_settled(admin, ep["id"], 1, timeout=30)[0]
        from hydra.integrations.webhooks import MAX_ATTEMPTS

        assert d["status"] == "dead" and d["attempts"] == MAX_ATTEMPTS
        assert all(r["signature_ok"] for r in rec.receipts)
        admin.json("DELETE", f"/v1/integrations/webhooks/{ep['id']}")
    # 4) a sender with the wrong key is rejected by the receiver (401 -> dead)
    with hc.Receiver(SECRET) as rec:
        ep = admin.add_webhook(rec.url, "wrong-secret-0123456789", ["ping"], name="kontrakt-badsig")
        d = admin.json("POST", f"/v1/integrations/webhooks/{ep['id']}/test", {})
        assert d["status"] == "dead" and d["last_status_code"] == 401 and rec.receipts[-1]["signature_ok"] is False
        admin.json("DELETE", f"/v1/integrations/webhooks/{ep['id']}")


def test_receiver_signature_rules(hc):
    body = b'{"x":1}'
    import hashlib
    import hmac

    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    h = {"x-hydra-timestamp": ts, "x-hydra-signature": sig}
    assert hc.verify_signature(SECRET, h, body)
    assert not hc.verify_signature(SECRET, h, body + b" ")
    assert not hc.verify_signature("other-secret-0123456789", h, body)
    assert not hc.verify_signature(SECRET, h, body, now=int(ts) + 301)  # replay outside the window


# ----------------------------------------------------------------------------------------------- graph export


def test_csv_cypher_roundtrip_counts_equal(live):
    res = live.client("viewer").neo4j_roundtrip()
    assert res["equal"] is True, res
    assert res["dangling_edges"] == 0
    assert res["zip_csv"]["nodes"].get("Incident", 0) > 0 and res["zip_csv"]["nodes"].get("Campaign", 0) > 0
    assert res["edge_total"] > 0


def test_roundtrip_detects_a_missing_row(live, hc):
    c = live.client("viewer")
    zdata = c.request("GET", "/v1/export/neo4j?format=zip")[2]
    nodes = c.request("GET", "/v1/export/neo4j?format=nodes_csv")[2].decode()
    edges = c.request("GET", "/v1/export/neo4j?format=edges_csv")[2].decode()
    cypher = c.request("GET", "/v1/export/neo4j?format=cypher")[2].decode()
    lines = edges.splitlines()
    assert hc.roundtrip_check(zdata, nodes, "\n".join(lines[:-1]) + "\n", cypher)["equal"] is False
