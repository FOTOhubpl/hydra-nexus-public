"""KONTRA fixes for the Astra review (2026-11-22, snapshot 489ac9a): ASTRA-DELTA-02/03/04/05/10 and §7.

Ported from the independent probes in `astra-delta-evidence-2026-11-22/test_delta.py` (there `xfail(strict)`); here
they are ordinary tests that pass after the fixes (CR-KONTRA-02..05, CR-KONTRA-10, CR-KONTRA-07). Synthetic data
only; every database write goes through the product `Writer` into the test's private temporary state.
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.request
import urllib.response
from email.message import Message

import pytest
from fastapi.testclient import TestClient

from hydra.db import Writer
from hydra.kontra import BROAD_LABEL_PL, experiment_status, freeze, rescore, run_experiment, save_experiment
from hydra.kontra_package import build_package, is_loopback_url, send_package, validate_ack
from tests.kontra.test_kontra import _tiny_experiment


def _delete_base_genes(settings, ids):
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda c: c.executemany("DELETE FROM genes WHERE incident_id=?", [(i,) for i in ids]))
    finally:
        writer.close()


# --------------------------------------------------------------------------------------- DELTA-02


def test_frozen_context_keeps_base_genomes(kontra_state):
    """ASTRA-DELTA-02: a re-extraction of the base after `freeze` does not leak into `rescore` of that context."""
    s = kontra_state["settings"]
    ctx = freeze(kontra_state["assign"].incident_id, s)
    a = rescore(ctx).assignment
    assert a.decision == "assign"
    _delete_base_genes(s, ctx.corpus_ids)
    b = rescore(ctx).assignment
    assert (a.decision, a.campaign_id) == (b.decision, b.campaign_id)
    assert [c.best_pair.total_bits for c in a.candidates] == [c.best_pair.total_bits for c in b.candidates]


def test_changed_corpus_marks_stored_experiment_stale(kontra_state):
    """The stored experiment is reported `stale_context` once the corpus moved; the API refuses a package of it."""
    from hydra.api import create_app

    s = kontra_state["settings"]
    inc = kontra_state["assign"].incident_id
    exp = run_experiment(inc, s)
    assert exp["status"] == "ok" and exp["context"]["frozen"]["materialised"] is True
    save_experiment(s, exp)
    assert experiment_status(s, exp)["status"] == "ok"
    _delete_base_genes(s, exp["context"]["frozen"]["corpus_ids"][:1])
    st = experiment_status(s, exp)
    assert st["status"] == "stale_context" and st["stale_reasons"] == ["corpus"]
    assert "nieaktualny" in st["status_pl"]
    with TestClient(create_app(s)) as c:
        got = c.get(f"/v1/lab/{inc}").json()
        assert got["status"] == "stale_context" and got["experiment_id"] == exp["experiment_id"]
        r = c.post(f"/v1/lab/{inc}/package", json={})
        assert r.status_code == 409 and r.json()["detail"]["status"] == "stale_context"


def test_legacy_experiment_without_frozen_context_is_stale(kontra_state):
    exp = {"incident_id": kontra_state["assign"].incident_id, "context": {"fingerprint": "x"}}
    assert experiment_status(kontra_state["settings"], exp)["stale_reasons"] == ["legacy"]


# --------------------------------------------------------------------------------------- DELTA-10


def _prior_file(tmp_path, name, df):
    from hydra import priors

    body = {"schema": priors.SCHEMA, "units": {"legit": 10000, "fraud": 0},
            "n_by_kind": {"tracker_id": {"legit": 10000, "fraud": 0}}, "columns": ["key", "df_legit", "df_fraud"],
            "df": [[priors.value_key("tracker_id", "1234567890123456"), df, 0]]}
    p = tmp_path / name
    p.write_bytes(gzip.compress(json.dumps(body).encode()))
    return str(p)


def test_frozen_context_pins_background_prior(kontra_state, tmp_path, monkeypatch):
    """ASTRA-DELTA-10: `rescore` uses the prior frozen at `freeze`, and the fingerprint identifies it."""
    p1, p2 = _prior_file(tmp_path, "before.json.gz", 0), _prior_file(tmp_path, "after.json.gz", 5000)
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", p1)
    ctx = freeze(kontra_state["assign"].incident_id, kontra_state["settings"])
    before = rescore(ctx).assignment
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", p2)
    after = rescore(ctx).assignment
    assert (before.decision, before.campaign_id) == (after.decision, after.campaign_id)
    other = freeze(ctx.incident_id, ctx.settings)
    assert other.fingerprint != ctx.fingerprint  # a different effective prior is a different context
    assert other.identity["background_prior"]["sha256"] != ctx.identity["background_prior"]["sha256"]
    assert ctx.scorer()["background_prior"]["mode"] == "mixed"
    # the freshly frozen context with the "common" prior decides differently: the pin above is what kept it
    assert str(rescore(other).assignment.decision) != "assign"


def test_prior_change_is_in_experiment_id_and_status(kontra_state, tmp_path, monkeypatch):
    s = kontra_state["settings"]
    inc = kontra_state["assign"].incident_id
    p1 = _prior_file(tmp_path, "a.json.gz", 0)
    p1b = _prior_file(tmp_path, "b.json.gz", 1)  # differs only in content
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", p1)
    e1 = run_experiment(inc, s)
    assert experiment_status(s, e1)["status"] == "ok"
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", p1b)
    assert experiment_status(s, e1)["stale_reasons"] == ["background_prior"]
    e2 = run_experiment(inc, s)
    assert e1["context"]["fingerprint"] != e2["context"]["fingerprint"]
    assert e1["experiment_id"] != e2["experiment_id"]
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "0")
    assert experiment_status(s, e1)["stale_reasons"] == ["background_prior"]


# --------------------------------------------------------------------------------------- DELTA-03


class _BodyOpener:
    def __init__(self, body: bytes):
        self.body = body

    def open(self, *args, **kwargs):
        return io.BytesIO(self.body)


@pytest.mark.parametrize("body", [
    b"{}", b"not json", b"[]", b"null", b"\xff\xfe",
    b'{"receipt_id": "rcpt-1", "package_id": "kp-someone-else", "duplicate": false}',
    b'{"receipt_id": "", "package_id": "@PID@", "duplicate": false}',
    b'{"receipt_id": 7, "package_id": "@PID@", "duplicate": false}',
    b'{"receipt_id": "rcpt-1", "package_id": "@PID@", "duplicate": "no"}',
    b'{"receipt_id": "rcpt-1", "package_id": "@PID@"}',
])
def test_invalid_ack_is_unconfirmed(monkeypatch, body):
    """ASTRA-DELTA-03: HTTP 200 with anything but a valid receipt of THIS package is `unconfirmed`."""
    pkg = build_package(_tiny_experiment())
    opener = _BodyOpener(body.replace(b"@PID@", pkg["package_id"].encode()))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: opener)
    r = send_package(pkg)
    assert r["status"] == "unconfirmed" and r["ack"] is None and "niepotwierdzony" in r["reason"]


def test_valid_ack_is_acknowledged(monkeypatch):
    pkg = build_package(_tiny_experiment())
    ack = {"receipt_id": "rcpt-1", "package_id": pkg["package_id"], "duplicate": False,
           "received_at": "2026-11-22T10:00:00Z", "synthetic_case_id": "SYN-CASE-1", "note": "demo"}
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: _BodyOpener(json.dumps(ack).encode()))
    r = send_package(pkg)
    assert r["status"] == "acknowledged" and r["ack"]["receipt_id"] == "rcpt-1"
    assert validate_ack({**ack, "duplicate": True}, pkg["package_id"]) is None


def test_timeout_after_send_is_unconfirmed(monkeypatch):
    class Slow:
        def open(self, *a, **k):
            raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Slow())
    assert send_package(build_package(_tiny_experiment()))["status"] == "unconfirmed"


# --------------------------------------------------------------------------------------- DELTA-04


def test_sender_does_not_follow_redirect_outside_loopback(monkeypatch):
    """ASTRA-DELTA-04: real urllib redirect machinery, transport intercepted before any socket or DNS."""
    calls = []

    class MemoryHTTP(urllib.request.HTTPHandler):
        def http_open(self, req):
            calls.append((req.get_method(), req.full_url))
            headers = Message()
            if req.host.startswith("127.0.0.1"):
                headers["Location"] = "http://outside.invalid/receive"
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                response.msg = "Found"
            else:
                response = urllib.response.addinfourl(io.BytesIO(b"{}"), headers, req.full_url, 200)
                response.msg = "OK"
            return response

    original = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: original(*a, MemoryHTTP()))
    result = send_package(build_package(_tiny_experiment()))
    assert calls == [("POST", "http://127.0.0.1:8765/receive")]
    assert result["status"] == "rejected" and "przekierowanie" in result["reason"]


@pytest.mark.parametrize("url", [
    "http://localhost:8765/receive",          # a name: the resolver / hosts file would decide
    "http://user@127.0.0.1:8765/receive",
    "http://127.0.0.1:99999/receive",
    "http://127.0.0.1:8765/receive#x",
    "http://127.0.0.1:8765/re ceive",
    "http://0.0.0.0:8765/receive",
    "http://[::ffff:10.0.0.1]/receive",
    "https://127.0.0.1/receive",
])
def test_loopback_validation_is_strict(url):
    assert not is_loopback_url(url)
    assert send_package(build_package(_tiny_experiment()), url)["status"] == "rejected"


def test_loopback_literals_are_accepted():
    assert is_loopback_url("http://127.0.0.1:8765/receive") and is_loopback_url("http://[::1]:9/x")
    assert is_loopback_url("http://127.0.0.2/receive")


# --------------------------------------------------------------------------------------- DELTA-05


def test_broad_family_removal_is_named_honestly(kontra_state):
    """ASTRA-DELTA-05: the family-wide removal is labelled a broad family-removal test, not a material-reuse model."""
    exp = run_experiment(kontra_state["assign"].incident_id, kontra_state["settings"])
    types = {r["type"] for r in exp["scenarios"]}
    classes = {r["class"] for r in exp["scenarios"]}
    assert "reuse_material" not in types and "reuse_hypothesis" not in classes
    broad = next(r for r in exp["scenarios"] if r["type"] == "broad_family_removal")
    assert broad["class"] == "diagnostic_stress" and broad["label_pl"] == BROAD_LABEL_PL
    assert "Szeroki test usunięcia rodzin" in broad["label_pl"] and "nie jest modelem kopiowania" in broad["label_pl"]
    assert "skopiowany" not in json.dumps(exp, ensure_ascii=False)
    # what the broad test removes is reported as is (here the phone group of the TXT family)
    assert broad["removed"]["groups"] == ["dependency_group:TXT:entity_phone"]


# --------------------------------------------------------------------------------------- §7 next observation


def test_next_observation_needs_its_artefact(kontra_state):
    exp = run_experiment(kontra_state["assign"].incident_id, kontra_state["settings"])
    nxt = exp["next_observations"]
    assert nxt["label_pl"] == "rekomendacja heurystyczna, bez wykazanej przewagi"
    assert "bez wykazanej przewagi" in nxt["note_pl"]
    assert exp["context"]["frozen"]["query_artefacts"] == {"har": False, "html": True, "image": False}
    ranked = {r["source"] for r in nxt["ranking"]}
    assert not ranked & {"har", "ocr", "image"}
    assert {r["requires"] for r in nxt["unavailable"]} == {"har", "image"}
