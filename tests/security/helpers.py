"""Small synthetic inputs; no organiser fixtures, learned models or external services."""

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib
from contextlib import closing

from hydra.contracts import Campaign, CampaignGroup, GeneMatch, PairScore
from hydra.db import Writer, connect
from hydra.ingest import import_paths, register_artifact
from hydra.jobs import JobRunner, create_job, get_job
from hydra.storage import ContentStore

STAMP = "2026-11-21T18:00:00Z"
TOKEN = "ASTRA_SYNTHETIC_CREDENTIAL_7q9"
SECRET_TEXT = f"synthetic https://campaign.example/?access_token={TOKEN}"


def run_import(settings, paths):
    writer = Writer(settings.db_path)
    runner = JobRunner(writer, 1)
    try:
        job_id = create_job(writer, "import", {})
        runner.submit(job_id, lambda ctx: import_paths(
            ctx, paths, settings, ContentStore(settings.cas_dir))).result(timeout=10)
    finally:
        runner.shutdown()
        writer.close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        job = get_job(conn, job_id)
        job["items"] = [dict(r) for r in conn.execute(
            "SELECT * FROM job_items WHERE job_id=? ORDER BY idx", (job_id,))]
    return job


def zip_bytes(entries, compression=zipfile.ZIP_STORED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buf.getvalue()


def oversized_png():
    """A tiny PNG with a 50,000 x 50,000 IHDR; never allocate its declared pixels."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 50_000, 50_000, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00"))
            + chunk(b"IEND", b""))


def seed(settings, *, title="Synthetic incident", value="operator-astra", campaign_id="C-astra",
         run_id="run-astra", date=STAMP, html=None, har=None):
    """Write a two-incident graph through the single Writer. No pipeline/model shortcuts are claimed."""
    reason = GeneMatch(family="INF", kind="operator_id", value_a=value, value_b=value, bits=12,
                       doc_freq=2, explanation_pl=f"Wspólny identyfikator: {value} (12 bitów)")
    campaign = Campaign(id=campaign_id, label=title, members=["astra-a", "astra-b"], confidence=0.8,
                        group_id="G-astra", first_seen=date, last_seen=date, scheme="investment",
                        core_genome=[reason])
    group = CampaignGroup(id="G-astra", label="Synthetic group", campaign_ids=[campaign_id], confidence=0.5)
    pair = PairScore(a="astra-a", b="astra-b", family_bits={"INF": 12}, reasons=[reason], total_bits=12,
                     probability=0.8, hypothesis="same_campaign", model="astra-synthetic@1")
    store = ContentStore(settings.cas_dir)
    artifacts = []
    for role, name, mime, data in [
        ("landing_html", "page.html", "text/html", html),
        ("har", "page.har", "application/json", har),
    ]:
        if data is not None:
            raw = data.encode() if isinstance(data, str) else data
            artifacts.append((store.put_bytes(raw), role, name, mime, len(raw)))

    def insert(conn):
        conn.execute("INSERT INTO runs(id,kind,created_at,finished_at) VALUES (?,'analyse',?,?)",
                     (run_id, STAMP, STAMP))
        for iid in campaign.members:
            conn.execute("INSERT INTO incidents(id,source,source_ref,title,observed_at,created_at)"
                         " VALUES (?,'astra-synthetic',?,?,?,?)", (iid, f"{iid}/page.html", title, date, STAMP))
            for kind, val in (("landing_host", "campaign.example"), ("operator_id", value)):
                conn.execute("INSERT INTO genes(incident_id,family,kind,value,status,provenance,extractor,"
                             "dependency_group,created_at) VALUES (?,'INF',?,?,'observed','OBS',"
                             "'astra-synthetic@1','INF:operator:test',?)", (iid, kind, val, STAMP))
            conn.execute("INSERT INTO campaign_members VALUES (?,?,?)", (run_id, campaign_id, iid))
        conn.execute("INSERT INTO campaigns(id,run_id,label,confidence,detail) VALUES (?,?,?,?,?)",
                     (campaign_id, run_id, title, 0.8, campaign.model_dump_json()))
        conn.execute("INSERT INTO campaign_groups(run_id,id,label,confidence,detail) VALUES (?,?,?,?,?)",
                     (run_id, group.id, group.label, group.confidence, group.model_dump_json()))
        conn.execute("INSERT INTO pairs(a,b,run_id,total_bits,probability,hypothesis,model,detail)"
                     " VALUES (?,?,?,12,0.8,'same_campaign','astra-synthetic@1',?)",
                     (pair.a, pair.b, run_id, pair.model_dump_json()))
        for digest, role, name, mime, size in artifacts:
            register_artifact(conn, digest=digest, mime=mime, size=size, source_ref=name, job_id="astra-seed")
            conn.execute("INSERT INTO incident_artifacts(incident_id,artifact_id,role,path_in_incident)"
                         " VALUES ('astra-a',?,?,?)", (digest, role, name))
    writer = Writer(settings.db_path)
    try:
        writer.run(insert)
    finally:
        writer.close()
    return campaign, run_id, artifacts


def decoded_package(data):
    """Include decoded evidence when looking for leaks; scanning base64 text alone is insufficient."""
    import base64

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        texts = [archive.read(name).decode("utf-8") for name in archive.namelist()]
    decoded = list(texts)
    for text in texts:
        if text.lstrip().startswith("{"):
            for evidence in json.loads(text).get("evidence", []):
                decoded.append(base64.b64decode(evidence["payload"]).decode("utf-8", "replace"))
    return "\n".join(decoded)
