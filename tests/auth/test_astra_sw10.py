"""CR-ASTRA-SW-10 (independent audit 2026-11-22, commit f450a70): the review author must be the authenticated
principal, not `body.analyst`. Reproducer moved from the audit bundle, assertions unchanged. Synthetic data only.

The fix is owned by the FIX-BACKEND session (branch fix/audit-backend, QA bug B2); this branch does not
reimplement it. The marker is strict: when that fix is merged these cases XPASS and the marker must be removed in
the same merge. Scope: misattributed authorship, not a demonstrated privilege escalation."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from tests.auth.conftest import FakeClock, Harness, auth_settings
from tests.security.helpers import seed


@pytest.mark.parametrize("endpoint", ["reviews", "review/unsure"])
@pytest.mark.parametrize("claimed_identity", [None, "admin"])
def test_sw10_review_actor_is_the_authenticated_analyst(tmp_path, endpoint, claimed_identity):
    settings = auth_settings(tmp_path)
    seed(settings)
    with TestClient(create_app(settings)) as client:
        harness = Harness(client, settings, FakeClock())
        harness.admin()
        analyst = harness.user("analyst", "real-analyst")
        try:
            body = {"target": {"a": "astra-a", "b": "astra-b"}, "note": "Synthetic actor audit"}
            if endpoint == "reviews":
                body["action"] = "accept_link"
            if claimed_identity is not None:
                body["analyst"] = claimed_identity
            response = analyst.post(f"/v1/{endpoint}", json=body)
            assert response.status_code == 201, response.text
            record = response.json()["review" if endpoint == "reviews" else "mark"]
            event_type = "REVIEW" if endpoint == "reviews" else "REVIEW_UNSURE"
            with client.app.state.hydra.reader() as conn:
                event = json.loads(conn.execute(
                    "SELECT payload FROM events WHERE type=? ORDER BY seq DESC LIMIT 1", (event_type,),
                ).fetchone()[0])
            assert record["analyst"] == "real-analyst"
            assert event["analyst"] == "real-analyst"
        finally:
            analyst.close()
