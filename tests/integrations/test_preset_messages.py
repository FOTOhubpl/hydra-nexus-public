"""Preset apply results: one clean sentence per step (steps_pl) and message_pl joined with ' · ', never '.;'."""

from __future__ import annotations

from hydra.api_integrations import _sentence


def test_sentence_cleanup():
    assert _sentence("Format pliku: stix21.;") == "Format pliku: stix21."
    assert _sentence("Konektor włączony") == "Konektor włączony."
    assert _sentence("Test udany (zapytania na żywo włączone).") == "Test udany (zapytania na żywo włączone)."
    assert _sentence("Gotowe..") == "Gotowe."


def test_apply_results_have_clean_sentences(offline_client):
    for preset in ("cert-misp", "siem", "ads-meta", "reputation"):
        r = offline_client.post(f"/v1/integrations/presets/{preset}/apply")
        assert r.status_code == 200, r.text
        for row in r.json()["results"]:
            assert ".;" not in row["message_pl"] and "; " not in row["message_pl"], row
            assert row["steps_pl"], row
            assert row["message_pl"] == " · ".join(row["steps_pl"]) or row["message_pl"] == "Gotowe."
            for step in row["steps_pl"]:
                assert step.endswith((".", "!", "?")), step
