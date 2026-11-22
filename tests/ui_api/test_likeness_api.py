"""Likeness triage card (hydra/likeness.py + /v1/incidents/{id}/likeness) on synthetic rows only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hydra import events
from hydra.api import create_app
from hydra.contracts import EvidencePointer, Gene
from hydra.db import Writer
from hydra.likeness import build_card, strength_of
from ui_api.synthetic import seed


def _g(inc, fam, kind, value, note=None, status="observed", prov="INF", art="a" * 64, group=None) -> Gene:
    return Gene(incident_id=inc, family=fam, kind=kind, value=value, status=status, provenance=prov,
                evidence=EvidencePointer(artifact_id=art, note=note, char_range=(0, 5)), extractor="test@1",
                dependency_group=group)


EXTRA = [
    _g("syn-1", "TXT", "person_named", "Jan Przykładowy", note="Jan Przykładowy (gazetteer:prior:businessman)",
       prov="OBS"),
    _g("syn-1", "TXT", "person_named", "Anna Zwykła", note="Anna Zwykła (heuristic:capitalised-given-name)"),
    _g("syn-1", "NAR", "promise", "passive_income", note="'5000 zł dziennie' (x1, material)"),
    _g("syn-1", "NAR", "media_brand", "tvn24", note="'TVN24' (x2, material)"),
    _g("syn-1", "META", "c2pa_manifest", "absent", note="brak manifestu C2PA w 1 sprawdzonych plikach",
       group="META:label:provenance"),
    _g("syn-1", "META", "wm_check", "detected", note="znak wodny generatora wykryty w 1 z 1 obrazów",
       group="META:label:provenance"),
    _g("syn-1", "META", "wm_generator", "sdxl_dwtdct", note="zgodność bitów 100%", group="META:label:provenance"),
    _g("syn-1", "ID", "avatar_geometry_check", None, status="unavailable", group="ID:label:synthetic_avatar"),
    _g("syn-1", "VIS", "pdq", "shared-pdq-value", note="unit=1"),
    _g("syn-2", "VIS", "pdq", "shared-pdq-value", note="unit=1"),
    _g("syn-6", "VIS", "pdq", "shared-pdq-value", note="logo", group="vis_brand"),  # boilerplate: ignored
]


def _insert(db_path, genes):
    w = Writer(db_path)
    now = events.utcnow()

    def tx(conn):
        for g in genes:
            conn.execute(
                "INSERT INTO genes (incident_id, family, kind, value, vector_ref, quality, status, provenance,"
                " evidence, extractor, dependency_group, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (g.incident_id, g.family, g.kind, g.value, None, g.quality, g.status, g.provenance,
                 events.canonical_json(g.evidence.model_dump()), g.extractor, g.dependency_group, now))

    try:
        w.run(tx)
    finally:
        w.close()


@pytest.fixture
def client(settings):
    seed(settings.db_path, settings.cas_dir)
    _insert(settings.db_path, EXTRA)
    with TestClient(create_app(settings)) as c:
        yield c


def test_card_full_signals(client):
    body = client.get("/v1/incidents/syn-1/likeness").json()
    sig = {s["key"]: s for s in body["signals"]}
    assert [s["key"] for s in body["signals"]] == ["public_figure", "financial_promise", "media_brand",
                                                    "reused_media", "ai_generator", "manipulation_suspicion"]
    assert sig["public_figure"]["state"] == "present"
    # seeded ID gallery match + gazetteer name; the capitalised-name heuristic alone is not a public figure
    assert sorted(i["value"] for i in sig["public_figure"]["items"]) == ["Jan Przykładowy", "Prezenter X"]
    txt = [i for i in sig["public_figure"]["items"] if i["family"] == "TXT"][0]
    assert txt["evidence"]["artifact_id"] == "a" * 64 and txt["evidence"]["char_range"] == [0, 5]
    assert sig["financial_promise"]["state"] == "present"
    assert sig["media_brand"]["state"] == "present"
    reuse = sig["reused_media"]
    assert reuse["state"] == "present" and {i["other_incident"] for i in reuse["items"]} == {"syn-2"}
    ai = sig["ai_generator"]
    checks = {c["key"]: c["state"] for c in ai["checks"]}
    assert checks == {"c2pa": "absent", "watermark": "present", "avatar_geometry": "unknown",
                      "audio_watermark": "unknown"}
    assert ai["state"] == "present"
    assert body["triggered"] is True and body["present"] == 5 and body["total"] == 5 and body["strength"] == "silne"
    assert sig["manipulation_suspicion"]["state"] == "unknown" and sig["manipulation_suspicion"]["counted"] is False
    assert "deepfake" in body["disclaimer_pl"] and body["limits_pl"]


def test_card_unknown_vs_absent(client):
    body = client.get("/v1/incidents/syn-4/likeness").json()
    sig = {s["key"]: s["state"] for s in body["signals"]}
    # syn-4 has TXT/ID/VIS genes but no NAR or prov genes: absent where a module ran, unknown where none did
    assert sig["public_figure"] == "absent"
    assert sig["financial_promise"] == "unknown"
    assert sig["ai_generator"] == "unknown"
    assert body["triggered"] is False
    assert any("Nie dało się sprawdzić" in x for x in body["limits_pl"])


def test_card_404_and_read_only(client, settings):
    assert client.get("/v1/incidents/nope/likeness").status_code == 404
    before = client.get("/v1/pairs").json()["total"]
    client.get("/v1/incidents/syn-1/likeness")
    assert client.get("/v1/pairs").json()["total"] == before


def test_build_card_pure_and_strength():
    card = build_card("x", [], [])
    assert card["present"] == 0 and card["strength"] == "brak"
    assert {s["state"] for s in card["signals"]} == {"unknown"}
    assert [strength_of(n) for n in range(6)] == ["brak", "słabe", "słabe", "umiarkowane", "silne", "silne"]
    reuse = [{"other": "y", "kind": "visual_near_duplicate", "value": None, "bits": 12.0,
              "evidence": {"artifact_id": "b" * 64, "bbox": [0, 0, 10, 10]}}]
    card = build_card("x", [], reuse)
    s = {s["key"]: s for s in card["signals"]}["reused_media"]
    assert s["state"] == "present" and s["items"][0]["evidence"]["bbox"] == [0, 0, 10, 10]


def test_lexicon_names_added_for_the_card():
    from hydra.genome.txt_entities import find_persons

    got = {e.value for e in find_persons("Robert Makłowicz i Adam Małysz polecają platformę")}
    assert got == {"Robert Makłowicz", "Adam Małysz"}
    assert not find_persons("szczęsny traf")  # lowercase common word is not the surname


def test_suspicion_block_never_raises_strength():
    lab = {"evidence": {"artifact_id": "c" * 64, "note": "sygnał podejrzenia manipulacji: podwyższony"}}
    genes = [{"family": "VIS", "kind": "synthetic_image_suspicion", "value": "high", "status": "observed", **lab},
             {"family": "VIS", "kind": "av_sync_suspicion", "value": "medium", "status": "observed", **lab},
             {"family": "META", "kind": "wm_audio_check", "value": "detected", "status": "observed", **lab},
             {"family": "META", "kind": "wm_audio", "value": "audioseal", "status": "observed", **lab}]
    card = build_card("x", genes, [])
    sig = {s["key"]: s for s in card["signals"]}
    sus = sig["manipulation_suspicion"]
    assert sus["state"] == "present" and not sus["counted"]
    assert {c["key"]: c["state"] for c in sus["checks"]} == {"av_sync": "absent", "synthetic_image": "present",
                                                            "synthetic_video": "unknown"}
    assert "deepfake" not in sus["summary_pl"].lower()
    ai = {c["key"]: c["state"] for c in sig["ai_generator"]["checks"]}
    # AudioSeal is a tool mark (CR-ASTRA-RES-1): shown as present, not a counted generator premise
    assert ai["audio_watermark"] == "present" and sig["ai_generator"]["state"] == "unknown"
    # nothing counted is present: strength 0 = "brak"; suspicion adds nothing
    assert card["present"] == 0 and card["strength"] == "brak" and card["total"] == 5
