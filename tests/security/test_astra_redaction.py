"""CR-ASTRA-SW-03 / SW-04 (independent audit 2026-11-22, commit f450a70): secrets in `<textarea>` content and in
cut HAR previews. Reproducers moved from the audit bundle unchanged; extra variants below. Synthetic data only."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hydra import api_artifacts
from hydra.api import create_app
from hydra.contracts import REDACTED
from hydra.export_abuse import abuse_package
from hydra.redact import redact_document, redact_text
from hydra.storage import ContentStore
from tests.security.helpers import decoded_package, seed

CANARY = "ASTRA_LOCAL_SYNTHETIC_SECRET_d73q"


def _surface(settings, surface, **seed_kw):
    campaign, run, artifacts = seed(settings, **seed_kw)
    if surface == "abuse":
        return decoded_package(abuse_package(settings, campaign.id, run)[0])
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/v1/artifacts/{artifacts[0][0]}/{surface}")
        assert response.status_code == 200
        return response.text


@pytest.mark.parametrize("surface", ["text", "raw", "content", "abuse"])
def test_sw03_textarea_secret_is_redacted_at_output_boundaries(settings, surface):
    html = f'<html><textarea name="password">{CANARY}</textarea><p>Visible evidence</p></html>'
    result = _surface(settings, surface, html=html)
    assert CANARY not in result


@pytest.mark.parametrize("surface", ["text", "raw", "content", "abuse"])
def test_control_input_secret_still_redacted(settings, surface):
    html = f'<html><input name="password" value="{CANARY}"><p>Visible evidence</p></html>'
    result = _surface(settings, surface, html=html)
    assert CANARY not in result
    assert "Visible evidence" in result


@pytest.mark.parametrize("html", [
    f'<TEXTAREA  NAME="Password" >{CANARY}</TEXTAREA>',
    f"<textarea\nname='api_key'\nrows=3>\n  {CANARY}\n</ textarea >",
    f'<textarea id="client_secret">{CANARY}',                       # unclosed, end of document
    f'<textarea type="text" autocomplete="new-password">{CANARY}<p>after</p>',  # unclosed, next tag
    f'<form><textarea name="access_token">{CANARY}</textarea><textarea name="comment">ok</textarea></form>',
])
def test_sw03_textarea_variants(html):
    out = redact_text(html)
    assert CANARY not in out
    assert REDACTED in out


def test_sw03_plain_textarea_content_stays_readable():
    html = '<textarea name="comment">Wiadomość od ofiary</textarea><textarea name="password"></textarea>'
    assert redact_text(html) == html


def test_sw03_cas_original_is_unchanged(settings):
    html = f'<html><textarea name="password">{CANARY}</textarea></html>'.encode()
    assert CANARY.encode() not in redact_document(html, "text/html")
    _, _, artifacts = seed(settings, html=html.decode())
    with TestClient(create_app(settings)) as client:
        assert CANARY not in client.get(f"/v1/artifacts/{artifacts[0][0]}/text").text
    assert ContentStore(settings.cas_dir).read_bytes(artifacts[0][0]) == html  # evidence bytes untouched


def _har(padding=3000):
    return json.dumps({"log": {"entries": [{"request": {
        "url": "https://audit.example/",
        "headers": [{"name": "X-Api-Key", "value": CANARY}, {"name": "Accept", "value": "text/html"}],
    }}]}, "padding": "x" * padding})


def test_sw04_truncated_har_preview_preserves_secret_redaction(settings):
    _, _, artifacts = seed(settings, har=_har())
    with TestClient(create_app(settings)) as client:
        route = f"/v1/artifacts/{artifacts[0][0]}/text"
        full = client.get(route)
        short = client.get(route, params={"max_bytes": 1024})
    assert full.status_code == short.status_code == 200
    assert CANARY not in full.text  # Control: structural redaction works before truncation.
    assert short.json()["truncated"] is True
    assert CANARY not in short.text
    assert "text/html" in short.json()["text"]  # non-secret header still visible
    assert len(short.json()["text"].encode()) <= 1024


def test_sw04_over_parse_budget_falls_back_to_text_pair_redaction(settings, monkeypatch):
    """Above the parse budget the document is not parsed whole; the cut window still loses the HAR secret."""
    monkeypatch.setattr(api_artifacts, "REDACT_PARSE_LIMIT", 1024)
    _, _, artifacts = seed(settings, har=_har())
    with TestClient(create_app(settings)) as client:
        short = client.get(f"/v1/artifacts/{artifacts[0][0]}/text", params={"max_bytes": 1024})
    assert short.status_code == 200 and short.json()["truncated"] is True
    assert CANARY not in short.text


@pytest.mark.parametrize("text", [
    f'{{"name": "Authorization", "value": "Bearer-{CANARY}"}}',
    f'{{"value": "{CANARY}", "name": "Cookie"}}',
    f'"name":"x-api-key","value":"{CANARY}',  # window cut inside the value
])
def test_sw04_har_pairs_redacted_as_text(text):
    assert CANARY not in redact_text(text)


def test_sw04_non_secret_har_pair_kept():
    text = '{"name": "Accept-Language", "value": "pl-PL"}'
    assert redact_text(text) == text
