"""422 bodies under /v1/integrations never echo submitted values (FastAPI's default `input` field would)."""

from __future__ import annotations


def test_too_long_connector_key_not_echoed(offline_client):
    value = "K" * 9000 + "UNIQUEMARK"
    r = offline_client.put("/v1/integrations/virustotal/secret", json={"name": "VT_API_KEY", "value": value})
    assert r.status_code == 422
    assert "UNIQUEMARK" not in r.text and "KKKKKKKK" not in r.text


def test_short_webhook_secret_and_url_not_echoed(offline_client):
    r = offline_client.post("/v1/integrations/webhooks", json={
        "name": "x", "url": "https://hooks.example.org/services/T000/B000/SECRETPATH", "events": ["ping"],
        "enabled": False, "secret": "shortSECRETMARK"})
    assert r.status_code == 422
    assert "SECRETMARK" not in r.text and "SECRETPATH" not in r.text


def test_other_paths_keep_default_validation_body(offline_client):
    r = offline_client.get("/v1/watch/events", params={"limit": "0"})
    assert r.status_code == 422
    assert "input" in r.text
