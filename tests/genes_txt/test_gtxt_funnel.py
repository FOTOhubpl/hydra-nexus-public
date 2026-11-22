"""Funnel-stage (modus operandi) genes on synthetic pages."""

from __future__ import annotations

import hashlib
import json

from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.genome import nar as _nar  # noqa: F401
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.nar import classify_fields
from hydra.genome.ocr_pool import OcrPool

PAGE = """<html lang="pl"><head><title>Szok w studiu TVN24</title></head><body>
<h1>Prezes aresztowany po wywiadzie na żywo. Wywiad usunięty</h1>
<p>Platforma pod nadzorem KNF: minimalna wpłata 950 zł, zarabiaj 3 000 zł miesięcznie.</p>
<p>Po rejestracji doradca skontaktuje się z Tobą telefonicznie. Wypłata środków w 24 godziny.</p>
<form action="/lander/order.php" method="post"><input name="name" placeholder="Imię">
<input name="phone" type="tel" placeholder="Numer telefonu"><input type="hidden" name="offer" value="x">
<input type="hidden" name="sub1" value="y"><button>Zarejestruj się</button></form>
<div class="comments"><div class="comment">Super, działa!</div><div class="comment">Polecam.</div></div>
</body></html>"""

WHITE = """<html><head><title>Konferencja 2026</title></head><body><h1>Konferencja o przyszłości</h1>
<p>Zapraszamy na wydarzenie w centrum miasta.</p></body></html>"""


def _inc(tmp_path, files):
    arts = []
    for name, role, data, mime in files:
        p = tmp_path / name
        p.write_bytes(data)
        arts.append(ResolvedArtifact(IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=role,
                                                      path_in_incident=name, mime=mime, size=len(data)), p))
    return ResolvedIncident(Incident(id="f1", source="t", source_ref="t", artifacts=[a.meta for a in arts]), arts)


def _genes(inc, settings, tmp_path):
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(None, engine_fn=lambda im: [])})
    return run_all(inc, ctx, only=["nar_funnel"]).genes


def test_full_funnel_sequence(settings, tmp_path):
    har = {"log": {"entries": [{"startedDateTime": "2026-10-09T06:25:00Z", "request": {
        "method": "POST", "url": "https://lead.example-scam.top/api/v1/trigger/field/abc", "headers": [],
        "postData": {"mimeType": "application/json", "text": json.dumps({"phone": "x", "first_name": "y"})}},
        "response": {"status": 200, "headers": [], "content": {}}}]}}
    inc = _inc(tmp_path, [("page.html", ArtifactRole.LANDING_HTML, PAGE.encode(), "text/html"),
                          ("x.har", ArtifactRole.HAR, json.dumps(har).encode(), "application/json")])
    genes = _genes(inc, settings, tmp_path)
    seq = next(g for g in genes if g.kind == "funnel_sequence")
    assert seq.value == "lure>trust>offer>capture>handoff>payment"
    stages = {g.value: g for g in genes if g.kind == "funnel_stage"}
    assert set(stages) == {"lure", "trust", "offer", "capture", "handoff", "payment"}
    assert all(g.dependency_group == "nar:mo" and g.family == "NAR" for g in genes)
    assert stages["capture"].evidence.artifact_id  # pointer to the form / HAR
    assert "cpa_hidden_fields" in stages["handoff"].evidence.note or "lead_post" in stages["handoff"].evidence.note
    fields = next(g for g in genes if g.kind == "funnel_capture_fields")
    assert {"name", "phone"} <= set(fields.value.split(","))


def test_white_page_has_short_sequence(settings, tmp_path):
    inc = _inc(tmp_path, [("w.html", ArtifactRole.LANDING_HTML, WHITE.encode(), "text/html")])
    genes = _genes(inc, settings, tmp_path)
    seq = [g.value for g in genes if g.kind == "funnel_sequence"]
    assert seq in ([], [""]) or "capture" not in seq[0]


def test_no_landing_marks_lower_quality(settings, tmp_path):
    txt = "PILNE: prezes aresztowany! Zarabiaj 3 000 zł miesięcznie, minimalna wpłata 1000 zł".encode()
    inc = _inc(tmp_path, [("chat.txt", ArtifactRole.TEXT, txt, "text/plain")])
    genes = _genes(inc, settings, tmp_path)
    seq = next(g for g in genes if g.kind == "funnel_sequence")
    assert seq.value.startswith("lure") and "payment" in seq.value and seq.quality < 0.5


def test_classify_fields():
    assert classify_fields(["first_name", "phone", "email", "_aio_handle"]) == {"name", "phone", "email"}
