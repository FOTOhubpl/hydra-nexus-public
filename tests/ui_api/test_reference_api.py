"""GALLERY API: POST /v1/reference/build (job with progress) + GET /v1/reference/status on a synthetic state."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from hydra.api import create_app
from ui_api.synthetic import seed


def _wait(c: TestClient, job_id: str) -> dict:
    for _ in range(200):
        j = c.get(f"/v1/reference/build/{job_id}").json()
        if j["status"] in ("done", "failed") and (j["result"] is not None or j["status"] == "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("reference build job did not finish")


def test_build_job_and_status(settings, monkeypatch):
    monkeypatch.delenv("HYDRA_VIS_REFERENCE_DIR", raising=False)
    seed(settings.db_path, settings.cas_dir)
    with TestClient(create_app(settings)) as c:
        s0 = c.get("/v1/reference/status").json()
        assert s0["face_entries"] == 0 and "lokalnie" in s0["explain_pl"]
        r = c.post("/v1/reference/build", json={"faces": False, "logos": True})
        assert r.status_code == 202
        j = _wait(c, r.json()["job_id"])
        assert j["status"] == "done" and j["total"] >= 1 and j["done"] >= 1
        assert "message_pl" in j["result"] and "path" not in j["result"]
        s1 = c.get("/v1/reference/status").json()
        assert s1["job"]["id"] == j["id"]
        assert c.get("/v1/reference/build/nope").status_code == 404
