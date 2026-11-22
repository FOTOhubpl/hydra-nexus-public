"""Artefact viewer API (hydra/api_artifacts.py) on synthetic rows only."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from hydra import events
from hydra.api import create_app
from hydra.config import Settings
from hydra.contracts import EvidencePointer, GeneMatch, PairScore
from hydra.db import Writer
from hydra.storage import ContentStore
from ui_api.synthetic import RUN_ID, seed

HTML = ('<html><head><title>Syntetyczna strona</title><script>alert(1)</script></head><body>'
        '<img src="./strona_files/hero.png"><img src="https://remote.example/x.png">'
        '<p>Zadzwoń +48 600 100 200</p></body></html>').encode()
HAR = json.dumps({"log": {"creator": {"name": "syn"}, "entries": [
    {"request": {"method": "GET", "url": "https://a.example/start?token=SECRET123"},
     "response": {"status": 302, "redirectURL": "https://b.example/land", "content": {"mimeType": "text/html"}},
     "time": 12.5},
    {"request": {"method": "GET", "url": "https://b.example/land"},
     "response": {"status": 200, "content": {"mimeType": "text/html; charset=utf-8", "size": 100}}},
    {"request": {"method": "GET", "url": "https://b.example/pixel.gif"},
     "response": {"status": 200, "content": {"mimeType": "image/gif", "size": 43}}},
]}}).encode()


def _add(settings: Settings) -> dict[str, str]:
    store = ContentStore(settings.cas_dir)
    from ui_api.synthetic import _png

    blobs = {"html": (HTML, "text/html", "landing_html", "strona.html"),
             "har": (HAR, "application/json", "har", "siec.har"),
             "hero": (_png((10, 20, 30), "hero"), "image/png", "landing_asset", "strona_files/hero.png"),
             "svg": (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/svg+xml",
                     "landing_asset", "strona_files/x.svg")}
    ids = {k: store.put_bytes(v[0]) for k, v in blobs.items()}
    now = events.utcnow()
    ad = None

    def tx(conn):
        nonlocal ad
        ad = conn.execute("SELECT artifact_id FROM incident_artifacts WHERE incident_id='syn-1'").fetchone()[0]
        for k, (data, mime, role, path) in blobs.items():
            conn.execute("INSERT OR IGNORE INTO artifacts (id, mime, size, source_ref, meta, created_at)"
                         " VALUES (?, ?, ?, ?, '{}', ?)", (ids[k], mime, len(data), f"/Users/someone/syn/{path}", now))
            conn.execute("INSERT INTO incident_artifacts (incident_id, artifact_id, role, path_in_incident, meta)"
                         " VALUES ('syn-1', ?, ?, ?, '{}')", (ids[k], role, path))
        conn.execute("INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence,"
                     " extractor, created_at) VALUES ('syn-1','TXT','entity_phone','+48600100200',1,'observed','OBS',"
                     " ?, 'txt_entities@1', ?)",
                     (events.canonical_json({"artifact_id": ids["html"], "char_range": [9, 24],
                                             "dom_path": "/html/body/p"}), now))
        conn.execute("INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence,"
                     " extractor, created_at) VALUES ('syn-1','TXT','asr_transcript',NULL,1,'unavailable','OBS',"
                     " ?, 'aud_asr@1', ?)", (events.canonical_json({"artifact_id": ad, "note": "ASR off"}), now))
        # the stored pair syn-1/syn-2 cites the creative of syn-1 with a bbox
        d = json.loads(conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a='syn-1' AND b='syn-2'",
                                    (RUN_ID,)).fetchone()[0])
        score = PairScore.model_validate(d)
        score.reasons.insert(0, GeneMatch(family="VIS", kind="pdq", value_a="syn-1-pdq", value_b="syn-2-pdq",
                                          bits=5.0, explanation_pl="Ten sam obraz",
                                          evidence_a=EvidencePointer(artifact_id=ad, bbox=(1, 2, 30, 40)),
                                          evidence_b=EvidencePointer(artifact_id=None)))
        conn.execute("UPDATE pairs SET detail=? WHERE run_id=? AND a='syn-1' AND b='syn-2'",
                     (score.model_dump_json(), RUN_ID))

    w = Writer(settings.db_path)
    try:
        w.run(tx)
    finally:
        w.close()
    ids["ad"] = ad
    return ids


@pytest.fixture
def ctx(settings: Settings):
    seed(settings.db_path, settings.cas_dir)
    ids = _add(settings)
    with TestClient(create_app(settings)) as c:
        yield c, ids, settings


def test_raw_serves_inert_types_and_never_html(ctx):
    c, ids, _ = ctx
    r = c.get(f"/v1/artifacts/{ids['ad']}/raw")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("inline; filename=")
    for k in ("html", "svg", "har"):
        r = c.get(f"/v1/artifacts/{ids[k]}/raw")
        assert r.headers["content-type"].startswith("text/plain"), k
        assert "sandbox" in r.headers["content-security-policy"]
    r = c.get(f"/v1/artifacts/{ids['html']}/raw", params={"download": 1})
    assert r.headers["content-disposition"].startswith('attachment; filename="strona.html"')
    assert c.get("/v1/artifacts/../../etc/raw").status_code == 404
    assert c.get(f"/v1/artifacts/{'0' * 63}g/raw").status_code == 400
    assert c.get(f"/v1/artifacts/{'0' * 64}/raw").status_code == 404


def test_text_preview_is_json_with_csp_and_local_assets(ctx):
    c, ids, _ = ctx
    body = c.get(f"/v1/artifacts/{ids['html']}/text", params={"preview": 1, "origin": "http://127.0.0.1:5400"}).json()
    assert body["kind"] == "html" and not body["truncated"]
    assert "<script>" in body["text"]  # raw source is shown as text
    doc = body["srcdoc"]
    assert doc.startswith('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'')
    assert f'src="/api/v1/artifacts/{ids["hero"]}/raw"' in doc and body["rewritten_assets"] == 1
    assert "img-src http://127.0.0.1:5400 data:" in doc
    evil = c.get(f"/v1/artifacts/{ids['html']}/text", params={"preview": 1, "origin": "https://evil.example"}).json()
    assert "evil.example" not in evil["srcdoc"]
    r = c.get(f"/v1/artifacts/{ids['hero']}/raw")
    assert "cross-origin-resource-policy" not in r.headers  # the sandboxed srcdoc frame has an opaque origin
    assert "https://remote.example/x.png" in doc  # left alone; blocked by the frame CSP
    assert c.get(f"/v1/artifacts/{ids['ad']}/text").status_code == 415
    small = c.get(f"/v1/artifacts/{ids['html']}/text", params={"max_bytes": 1024}).json()
    assert not small["truncated"]


def test_info_genes_and_pairs(ctx):
    c, ids, _ = ctx
    info = c.get(f"/v1/artifacts/{ids['ad']}/info").json()
    assert info["kind"] == "image" and info["media"]["width"] == 320
    assert info["incidents"][0]["incident_id"] == "syn-1"
    html = c.get(f"/v1/artifacts/{ids['html']}/info").json()
    assert html["source_ref"].startswith("~/") and "someone" not in json.dumps(html)
    g = c.get(f"/v1/artifacts/{ids['ad']}/genes").json()
    assert g["run_id"] == RUN_ID and g["incidents_in_run"] == 6
    pdq = next(x for x in g["genes"] if x["kind"] == "pdq")
    assert pdq["doc_freq"] == 1 and pdq["rarity_bits"] is not None
    assert pdq["pairs"] and pdq["pairs"][0]["b"] == "syn-2" and pdq["pairs"][0]["evidence"]["bbox"] == [1, 2, 30, 40]
    assert any(x["status"] == "unavailable" for x in g["genes"])
    assert "TRUTH" not in json.dumps(g)


def test_har_table_redirect_chain_and_redaction(ctx):
    c, ids, _ = ctx
    h = c.get(f"/v1/artifacts/{ids['har']}/har").json()
    assert h["entries"] == 3 and h["statuses"] == {"302": 1, "200": 2}
    assert [s["host"] for s in h["redirect_chains"][0]] == ["a.example", "b.example"]
    assert "SECRET123" not in json.dumps(h)
    assert h["rows"][1]["mime"] == "text/html"
    assert c.get(f"/v1/artifacts/{ids['html']}/har").status_code == 422


def test_transcript_unknown_when_asr_did_not_run(ctx):
    c, ids, settings = ctx
    t = c.get(f"/v1/artifacts/{ids['ad']}/transcript").json()
    assert t["available"] is False and t["gene"]["status"] == "unavailable"
    d = settings.cache_dir / "asr"
    d.mkdir(parents=True)
    (d / f"{ids['ad']}.whisper-small.v1.json").write_text(json.dumps(
        {"model": "faster-whisper-small", "segments": [{"start": 0.5, "end": 2.0, "text": "Dzień dobry"}]}))
    t = c.get(f"/v1/artifacts/{ids['ad']}/transcript").json()
    assert t["available"] and t["segments"][0]["text"] == "Dzień dobry"


def test_thumbnail_cached_under_work_dir(ctx):
    c, ids, settings = ctx
    r = c.get(f"/v1/artifacts/{ids['ad']}/thumb", params={"w": 150})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (settings.work_dir / "thumbs" / f"{ids['ad']}.w160.jpg").is_file()
    assert c.get(f"/v1/artifacts/{ids['html']}/thumb").status_code == 415
    assert c.get(f"/v1/artifacts/{ids['ad']}/frame", params={"t": 1}).status_code == 415


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_video_frame_and_probe(ctx, tmp_path):
    c, _, settings = ctx
    mp4 = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5:duration=2",
                    "-pix_fmt", "yuv420p", str(mp4)], check=True, timeout=60)
    digest, size = ContentStore(settings.cas_dir).put_file(mp4)
    w = Writer(settings.db_path)
    try:
        w.run(lambda conn: conn.execute("INSERT INTO artifacts (id, mime, size, source_ref, meta, created_at) VALUES"
                                        " (?, 'video/mp4', ?, 'v.mp4', '{}', ?)", (digest, size, events.utcnow())))
    finally:
        w.close()
    r = c.get(f"/v1/artifacts/{digest}/frame", params={"t": 1.23})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (settings.work_dir / "thumbs" / f"{digest}.t12.w320.jpg").is_file()
    info = c.get(f"/v1/artifacts/{digest}/info").json()
    assert info["kind"] == "video" and info["media"]["width"] == 64 and info["media"]["duration_s"] > 1.5


def test_materials_summary_and_local_guard(ctx):
    c, ids, _ = ctx
    m = c.get("/v1/materials").json()
    assert m["total"] == 10 and m["by_kind"]["image"] == 7 and m["by_kind"]["html"] == 1
    assert m["by_corpus"] == {"nask": 10}  # synthetic source maps to the default corpus
    assert all(r["role"] != "landing_asset" for r in m["recent"])
    assert c.get("/v1/materials", headers={"host": "evil.example"}).status_code == 403
    assert c.post(f"/v1/artifacts/{ids['ad']}/raw").status_code in (403, 405)
