"""Synthetic workflow acceptance through real extractors and public product APIs.

These tests measure orchestration and stated fixture behavior, not hidden-set accuracy.
No extractor, vector, score, threshold, retrieval result or campaign is replaced.
"""

from __future__ import annotations

import csv
import json
import zipfile
from contextlib import closing

import pytest

from hydra.config import get_settings
from hydra.db import Writer, connect
from hydra.events import verify_chain
from hydra.export_neo4j import export_run
from hydra.export_stix import export_stix
from hydra.pipeline import (
    analyse,
    analyse_input,
    assign_new,
    assign_stored,
    explain_assignment,
    load_clustering,
)
from hydra.retrieval import candidates
from hydra.review import apply_review, new_review
from hydra.storage import ContentStore
from tests.pipeline_audit.helpers import REAL_WEB_EXTRACTORS, creative_bytes, make_capture, sha256, web_corpus


@pytest.fixture(autouse=True)
def synthetic_boundary(monkeypatch, settings):
    """Extend the shared resource guard for implicit settings and serial extraction."""
    for name, value in {
        "HYDRA_ROOT": settings.root, "HYDRA_EXTRACT_WORKERS": "0",
    }.items():
        monkeypatch.setenv(name, str(value))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _members(result):
    return {frozenset(c.members): c for c in result.clustering.campaigns}


def _assert_evidence_resolves(score, settings):
    store = ContentStore(settings.cas_dir)
    pointed = []
    for reason in score.reasons:
        assert reason.explanation_pl.strip()
        for evidence in (reason.evidence_a, reason.evidence_b):
            if evidence is not None and evidence.artifact_id:
                digest = evidence.artifact_id
                assert sha256(store.read_bytes(digest)) == digest
                pointed.append(digest)
    assert pointed, "a link must retain pointers to the original captured bytes"


def test_real_web_capture_to_campaign_assignment_evidence_and_exports(settings, tmp_path):
    """Rotated domains share real parsed operator IDs; an unrelated capture stays open-set."""
    result = analyse([web_corpus(tmp_path / "corpus")], settings, only=REAL_WEB_EXTRACTORS,
                     options={"asr": False}, prefix="pipeline-audit")
    a_members = frozenset({"pipeline-audit-a1", "pipeline-audit-a2"})
    b_members = frozenset({"pipeline-audit-b1", "pipeline-audit-b2"})
    campaigns = _members(result)
    assert a_members in campaigns and b_members in campaigns
    assert all(len(members) == 1 for members in campaigns if members not in (a_members, b_members))
    a_campaign = campaigns[a_members]
    pair_key = tuple(sorted(a_members))
    pair = result.pairs[pair_key]
    assert pair.score.hypothesis == "same_campaign"
    assert {r.kind for r in pair.score.reasons} & {"operator_id", "tracker_id", "entity_phone"}
    assert all(not genome.vectors for genome in result.genomes.values())
    assert {"INF", "TXT"} <= {str(g.family) for genome in result.genomes.values() for g in genome.genes}
    assert all(g.extractor.split("@")[0] in REAL_WEB_EXTRACTORS
               for genome in result.genomes.values() for g in genome.genes)
    # The small batch uses all-pairs. Check real extracted operator evidence also retrieves the pair
    # through the actual index API when that fallback is explicitly disabled.
    indexed = candidates(result.genomes, result.rarity, all_pairs_below=0)
    assert pair_key in indexed and "all_pairs" not in indexed[pair_key]
    assert indexed[pair_key] & {"exact:operator_id", "exact:tracker_id", "exact:entity_phone"}
    _assert_evidence_resolves(pair.score, settings)

    variant = make_capture(tmp_path / "rotated-domain", 1, "new-domain")
    assignment_run, assigned = assign_new(variant, settings, base_run=result.run_id, only=REAL_WEB_EXTRACTORS)
    assert assigned.decision == "assign" and assigned.campaign_id == a_campaign.id
    assert assigned.reason_pl and assigned.candidates
    _assert_evidence_resolves(assigned.candidates[0].best_pair, settings)
    replay = assign_stored(assigned.incident_id, settings, base_run=result.run_id)
    assert (replay.assignment.decision, replay.assignment.campaign_id) == ("assign", a_campaign.id)
    explained = explain_assignment(assigned.incident_id, settings)
    assert explained["run_id"] == assignment_run and explained["assignment"]["reason_pl"] == assigned.reason_pl

    unknown = make_capture(tmp_path / "unknown", 99, "unrelated", har=False)
    _, rejected = assign_new(unknown, settings, base_run=result.run_id, only=REAL_WEB_EXTRACTORS)
    assert rejected.decision == "new_campaign" and rejected.campaign_id is None
    assert rejected.reason_pl
    # Proposals never silently alter the analysed campaign; approval is a separate public operation.
    with closing(connect(settings.db_path, readonly=True)) as conn:
        original, _ = load_clustering(conn, result.run_id)
        assert set(next(c for c in original if c.id == a_campaign.id).members) == a_members
        assert verify_chain(conn)[0] is True
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (result.job_id,)).fetchone()[0] == "done"
        assert conn.execute("SELECT COUNT(*) FROM gene_vectors").fetchone()[0] == 0
        finished = [json.loads(r["payload"])["run_id"] for r in conn.execute(
            "SELECT payload FROM events WHERE type='RUN_FINISHED'")]
        assert result.run_id in finished and assignment_run in finished
        counts = export_run(conn, result.run_id, tmp_path / "neo4j")
    assert counts["incidents"] == len(result.incidents)
    assert counts["member_of"] == len(result.incidents)
    with (tmp_path / "neo4j" / "incidents.csv").open(newline="") as stream:
        assert {row["id"] for row in csv.DictReader(stream)} == set(result.incidents)
    with (tmp_path / "neo4j" / "linked_to.csv").open(newline="") as stream:
        links = list(csv.DictReader(stream))
    assert len(links) == len(result.pairs) == counts["linked_to"]
    assert all({row["a"], row["b"]} <= set(result.incidents) for row in links)
    bundle = export_stix(settings, a_campaign.id, result.run_id)
    stix_campaign = next(o for o in bundle["objects"] if o["type"] == "campaign")
    assert {ref["external_id"] for ref in stix_campaign["external_references"]} == a_members
    object_ids = {obj["id"] for obj in bundle["objects"]}
    for obj in bundle["objects"]:
        if obj["type"] == "relationship":
            assert {obj["source_ref"], obj["target_ref"]} <= object_ids


def test_review_publishes_and_undo_restores_real_extractor_membership(settings, tmp_path):
    dataset = tmp_path / "duplicates"
    payload = creative_bytes()
    for name in ("a", "b"):
        folder = dataset / name
        folder.mkdir(parents=True)
        (folder / "reklama.png").write_bytes(payload)
    result = analyse([dataset], settings, only=["artifact_sha256"], prefix="pipeline-audit")
    pair = frozenset(result.incidents)
    assert pair in _members(result)
    review = new_review("reject_link", {"a": "pipeline-audit-a", "b": "pipeline-audit-b"},
                        analyst="synthetic-reviewer",
                        note="Independent synthetic operator evidence overrides duplicate.")
    writer = Writer(settings.db_path)
    try:
        split_run = apply_review(review, settings, writer, base_run=result.run_id)
        restored_run = apply_review(new_review("undo", {}, undoes=review.id), settings, writer,
                                    base_run=split_run)
    finally:
        writer.close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        split, _ = load_clustering(conn, split_run)
        restored, _ = load_clustering(conn, restored_run)
        assert all(len(c.members) == 1 for c in split)
        assert {frozenset(c.members) for c in restored} == {pair}
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='REVISION_PUBLISHED'").fetchone()[0] == 2
        assert verify_chain(conn)[0] is True


def test_same_zip_bytes_have_one_identity_across_names_and_dispatchers(settings, tmp_path):
    packet = tmp_path / "packet.zip"
    payload = creative_bytes()
    with zipfile.ZipFile(packet, "w") as archive:
        archive.writestr("incident/reklama.png", payload)
    renamed = tmp_path / "renamed.zip"
    renamed.write_bytes(packet.read_bytes())
    first = analyse_input("file", packet, settings, only=["artifact_sha256"])
    _, second = assign_new(renamed, settings, only=["artifact_sha256"])
    assert first["incident_id"] == second.incident_id
    assert first["assignment"]["decision"] == second.decision == "new_campaign"
    assert first["assignment"]["meta"]["onboarding"] is True
    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM incident_artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 2
        assert verify_chain(conn)[0] is True
    assert ContentStore(settings.cas_dir).read_bytes(sha256(payload)) == payload


def test_corrupt_har_and_missing_page_leave_visible_unknowns_with_valid_sibling(settings, tmp_path):
    dataset = tmp_path / "mixed"
    make_capture(dataset / "valid", 1, "valid")
    damaged = dataset / "damaged"
    damaged.mkdir()
    (damaged / "capture.har").write_bytes(b'{"log":{"entries":[BROKEN')
    (damaged / "empty.txt").write_bytes(b"")
    result = analyse([dataset], settings, only=["inf_url", "inf_har", "inf_dom"], prefix="pipeline-audit")
    assert set(result.incidents) == {"pipeline-audit-valid", "pipeline-audit-damaged"}
    good, bad = result.genomes["pipeline-audit-valid"], result.genomes["pipeline-audit-damaged"]
    assert any(g.kind == "tracker_id" and g.status == "observed" for g in good.genes)
    assert bad.genes and all(g.status in {"unavailable", "failed", "not_applicable"} for g in bad.genes)
    assert all(len(c.members) == 1 for c in result.clustering.campaigns)
    pair = next(iter(result.pairs.values()))
    assert pair.score.hypothesis != "same_campaign" and pair.score.contradictions == []
    assert "INF" in pair.score.missing_families
    with closing(connect(settings.db_path, readonly=True)) as conn:
        stored = conn.execute("SELECT status FROM genes WHERE incident_id='pipeline-audit-damaged'").fetchall()
        assert stored and all(row["status"] != "observed" for row in stored)
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (result.job_id,)).fetchone()[0] == "done"
        assert verify_chain(conn)[0] is True


def test_nonexistent_single_input_fails_job_without_partial_incident_or_success_run(settings, tmp_path):
    with pytest.raises(FileNotFoundError):
        analyse_input("file", tmp_path / "does-not-exist.html", settings, only=["inf_dom"])
    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        jobs = conn.execute("SELECT status FROM jobs WHERE kind='analyse_input'").fetchall()
        assert jobs and all(row["status"] == "failed" for row in jobs)
        assert conn.execute("SELECT COUNT(*) FROM events WHERE type='RUN_FINISHED'").fetchone()[0] == 0
        assert verify_chain(conn)[0] is True


def test_missing_ocr_weights_remain_unavailable_after_full_persistence(settings, tmp_path):
    """An absent model is unknown observation, not a malformed input failure."""
    folder = make_capture(tmp_path / "with-screenshot", 1, "ocr-unavailable", har=False)
    (folder / "reklama.png").write_bytes(creative_bytes())
    result = analyse([folder], settings, only=["txt_lines", "inf_dom"],
                     options={"asr": False}, prefix="pipeline-audit")
    genome = next(iter(result.genomes.values()))
    missing_model = [g for g in genome.genes if "ModelUnavailable" in (g.evidence.note or "")]
    assert missing_model, "empty model directories must produce an explicit unknown OCR observation"
    assert any(g.kind == "tracker_id" and g.status == "observed" for g in genome.genes)
    assert all(g.status == "unavailable" for g in missing_model)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        rows = conn.execute("SELECT status FROM genes WHERE evidence LIKE '%ModelUnavailable%'").fetchall()
        assert rows and all(row["status"] == "unavailable" for row in rows)


def test_datasets_with_same_folder_names_preserve_old_run_and_originals(settings, tmp_path):
    payload = creative_bytes()
    old_root, new_root = tmp_path / "first-batch", tmp_path / "second-batch"
    for name in ("1", "2"):
        old_folder, new_folder = old_root / name, new_root / name
        old_folder.mkdir(parents=True)
        new_folder.mkdir(parents=True)
        (old_folder / "reklama.png").write_bytes(payload)
        (new_folder / "reklama.png").write_bytes(creative_bytes(seed=int(name)))
    first = analyse([old_root], settings, only=["artifact_sha256"], prefix="pipeline-audit")
    with closing(connect(settings.db_path, readonly=True)) as conn:
        export_run(conn, first.run_id, tmp_path / "before")
    second = analyse([new_root], settings, only=["artifact_sha256"], prefix="pipeline-audit")
    assert len(first.incidents) == len(second.incidents) == 2
    assert set(first.incidents).isdisjoint(second.incidents)
    assert frozenset(first.incidents) in _members(first)
    assert all(len(c.members) == 1 for c in second.clustering.campaigns)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        export_run(conn, first.run_id, tmp_path / "after")
        for iid in first.incidents:
            artifacts = conn.execute("SELECT artifact_id FROM incident_artifacts WHERE incident_id=?", (iid,))
            assert {r["artifact_id"] for r in artifacts} == {sha256(payload)}
        assert verify_chain(conn)[0] is True
    for original in (tmp_path / "before").glob("*.csv"):
        assert original.read_bytes() == (tmp_path / "after" / original.name).read_bytes()


def test_colliding_short_path_hashes_cannot_merge_distinct_capture_roots(settings, tmp_path):
    """Construct distinct folder names with a real 24-bit disambiguator collision."""
    seen = {}
    collision = None
    for index in range(100_000):
        candidate = tmp_path / f"synthetic-upload-{index}" / "1"
        short = sha256(str(candidate).encode())[:6]
        if short in seen:
            collision = (seen[short], candidate)
            break
        seen[short] = candidate
    if collision is None:
        raise RuntimeError("No 24-bit fixture collision found within the bounded synthetic search")
    roots = [tmp_path / "anchor-upload" / "1", *collision]
    for index, folder in enumerate(roots):
        folder.mkdir(parents=True)
        (folder / "reklama.png").write_bytes(creative_bytes(seed=index))
    result = analyse(roots, settings, only=["artifact_sha256"], prefix="pipeline-audit")
    assert len(result.incidents) == 3, "distinct roots must retain distinct incident identities"
    with closing(connect(settings.db_path, readonly=True)) as conn:
        refs = {r["source_ref"] for r in conn.execute("SELECT source_ref FROM incidents")}
        assert refs == {str(folder) for folder in roots}
        assert conn.execute("SELECT COUNT(*) FROM incident_artifacts").fetchone()[0] == 3
    # Reimport of the same roots is idempotent: every root keeps the id it owns, nothing merges or duplicates.
    again = analyse(list(reversed(roots)), settings, only=["artifact_sha256"], prefix="pipeline-audit")
    assert set(again.incidents) == set(result.incidents)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        owners = {r["id"]: r["source_ref"] for r in conn.execute("SELECT id, source_ref FROM incidents")}
        assert len(owners) == 3 and set(owners.values()) == {str(folder) for folder in roots}
        assert conn.execute("SELECT COUNT(*) FROM incident_artifacts").fetchone()[0] == 3
