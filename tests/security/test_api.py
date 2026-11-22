"""In-process ASGI requests: local guard, hostile artifact identifiers and browser response headers."""

import hashlib
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.db import Writer
from hydra.ingest import register_artifact
from hydra.storage import ContentStore
from tests.security.helpers import seed


@pytest.mark.parametrize("prefix", ["", "/api"])
@pytest.mark.parametrize("host", ["attacker.example", "localhost.attacker.example", "127.0.0.1.attacker.example"])
def test_rebinding_hosts_are_blocked(client, prefix, host):
    assert client.get(f"{prefix}/v1/artifacts", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("prefix", ["", "/api"])
def test_cross_origin_writes_rejected_before_route_dispatch(client, method, prefix):
    result = client.request(method, f"{prefix}/v1/import/paths", json={"paths": []},
                            headers={"Origin": "https://attacker.example"})
    assert result.status_code == 403


@pytest.mark.parametrize("headers", [
    {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://localhost.attacker.example"},
    {"Host": "attacker.example", "X-Forwarded-Host": "localhost", "X-Forwarded-For": "127.0.0.1"},
])
def test_null_origins_and_forwarded_headers_do_not_bypass_guard(client, headers):
    assert client.post("/api/v1/import/paths", json={"paths": []}, headers=headers).status_code == 403


def test_forwarded_headers_do_not_override_real_local_host(client):
    result = client.get("/api/v1/artifacts", headers={
        "Host": "localhost:8000", "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-For": "169.254.169.254", "X-Forwarded-Proto": "https",
    })
    assert result.status_code == 200


@pytest.mark.parametrize("origin", ["http://[", "http://[not-ipv6]", "http://[::1]@attacker.example"])
def test_malformed_origin_is_rejected_without_server_error(client, origin):
    result = client.post("/v1/import/paths", json={"paths": []}, headers={"Origin": origin})
    assert 400 <= result.status_code < 500


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE", "CONNECT"])
def test_unimplemented_methods_do_not_start_jobs(client, method):
    result = client.request(method, "/api/v1/import")
    assert result.status_code in {404, 405}
    assert client.get("/v1/jobs").json() == []


@pytest.mark.parametrize("mime", [
    "text/html", "image/svg+xml", "application/xml", "application/xhtml+xml",
])
def test_active_markup_is_served_inert_with_security_headers(settings, mime):
    raw = b"<html><script>ASTRA_SENTINEL()</script></html>"
    store = ContentStore(settings.cas_dir)
    digest = store.put_bytes(raw)
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda conn: register_artifact(
            conn, digest=digest, mime=mime, size=len(raw), source_ref="synthetic", job_id="astra"))
    finally:
        writer.close()
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/v1/artifacts/{digest}/content")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["content-type"].split(";")[0] in {"text/plain", "application/octet-stream"}
    assert response.content == raw


@pytest.mark.parametrize("identifier", ["../outside", "%2e%2e%2foutside", "a" * 64, "*", "%", "' OR 1=1--"])
def test_invalid_or_absent_artifact_ids_cannot_read_files(client, identifier):
    for suffix in ("", "/content"):
        assert client.get(f"/v1/artifacts/{identifier}{suffix}").status_code in {400, 404, 422}


def test_cas_symlink_cannot_serve_outside_file(settings, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("ASTRA_OUTSIDE_SENTINEL")
    digest = hashlib.sha256(b"synthetic-original").hexdigest()
    path = ContentStore(settings.cas_dir).path_for(digest)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda conn: register_artifact(
            conn, digest=digest, mime="text/plain", size=1, source_ref="synthetic", job_id="astra"))
    finally:
        writer.close()
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/v1/artifacts/{digest}/content")
    assert response.status_code in {400, 403, 404}
    assert "ASTRA_OUTSIDE_SENTINEL" not in response.text


def test_abuse_download_disposition_has_no_injected_headers(settings):
    malicious = 'C-astra"\r\nX-Astra-Injected: yes'
    seed(settings, campaign_id=malicious)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get(f"/v1/campaigns/{quote(malicious, safe='')}/abuse-package")
    assert response.status_code != 500
    assert all("\r" not in value and "\n" not in value for value in response.headers.values())
    assert "x-astra-injected" not in response.headers
