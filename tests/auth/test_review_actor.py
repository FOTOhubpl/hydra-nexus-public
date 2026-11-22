"""AUDIT B2 / CR-ASTRA-SW-10: analyst decisions are recorded as the authenticated principal, never as a name the
client puts in the body; the hash-chained audit event carries the same user. With HYDRA_AUTH=0 the body name (or
"operator") is kept (single-user local mode)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from tests.auth.conftest import Harness


def _last_event(client: TestClient, kind: str) -> dict:
    with client.app.state.hydra.reader() as conn:
        return json.loads(conn.execute("SELECT payload FROM events WHERE type=? ORDER BY seq DESC LIMIT 1",
                                       (kind,)).fetchone()[0])


@pytest.mark.parametrize("claimed", [None, "admin", "operator"])
def test_review_records_authenticated_user(h: Harness, claimed):
    h.admin()
    analyst = h.user("analyst", "real-analyst")
    body = {"action": "accept_link", "target": {"a": "x-a", "b": "x-b"}}
    if claimed is not None:
        body["analyst"] = claimed
    r = analyst.post("/v1/reviews", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["review"]["analyst"] == "real-analyst"
    assert _last_event(h.c, "REVIEW")["analyst"] == "real-analyst"
    with h.c.app.state.hydra.reader() as conn:
        assert conn.execute("SELECT analyst FROM reviews WHERE id=?",
                            (r.json()["review"]["id"],)).fetchone()[0] == "real-analyst"
    # undo is attributed too
    u = analyst.post("/v1/reviews", json={"action": "undo", "undoes": r.json()["review"]["id"], "analyst": "admin"})
    assert u.status_code == 201, u.text
    assert u.json()["review"]["analyst"] == "real-analyst"
    assert analyst.get("/v1/audit/verify").json().get("ok") is True


@pytest.mark.parametrize("claimed", [None, "admin"])
def test_unsure_mark_and_undo_record_authenticated_user(h: Harness, claimed):
    h.admin()
    analyst = h.user("analyst", "real-analyst")
    body = {"target": {"a": "x-a", "b": "x-b"}}
    if claimed is not None:
        body["analyst"] = claimed
    r = analyst.post("/v1/review/unsure", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["mark"]["analyst"] == "real-analyst"
    assert _last_event(h.c, "REVIEW_UNSURE")["analyst"] == "real-analyst"
    u = analyst.post("/v1/review/unsure/undo", json={"id": r.json()["mark"]["id"], "analyst": "admin"})
    assert u.status_code in (200, 201), u.text
    with h.c.app.state.hydra.reader() as conn:
        payload = json.loads(conn.execute("SELECT payload FROM events ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert payload.get("analyst") == "real-analyst"


def test_api_token_user_is_recorded(h: Harness):
    h.admin()
    analyst = h.user("analyst", "token-owner")
    t = analyst.post("/v1/auth/tokens", json={"name": "ci"})
    assert t.status_code == 201, t.text
    bearer = h.client()
    bearer.headers["Authorization"] = f"Bearer {t.json()['token']}"
    r = bearer.post("/v1/reviews", json={"action": "reject_link", "target": {"a": "y-a", "b": "y-b"},
                                         "analyst": "someone-else"})
    assert r.status_code == 201, r.text
    assert r.json()["review"]["analyst"] == "token-owner"


def test_without_auth_body_name_is_kept(settings):
    assert not settings.auth
    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "z-a", "b": "z-b"},
                                        "analyst": "jan"})
        assert r.status_code == 201, r.text
        assert r.json()["review"]["analyst"] == "jan"
        r = c.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "z-c", "b": "z-d"}})
        assert r.json()["review"]["analyst"] == "operator"
