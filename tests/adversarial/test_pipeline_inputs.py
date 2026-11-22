"""Synthetic file failures and duplicates through the real import/extract/score pipeline."""

from hydra.db import connect
from hydra.pipeline import analyse


def test_empty_directory_does_not_crash_pipeline(settings, tmp_path):
    empty = tmp_path / "empty-input"
    empty.mkdir()
    result = analyse([empty], settings, only=["artifact_sha256"], use_cache=False, prefix="astra")
    # The adapter may retain an empty incident; the acceptance criterion is graceful unknown data,
    # not a requirement to discard that incident.
    assert all(not genome.genes for genome in result.genomes.values())
    assert all(len(c.members) == 1 for c in result.clustering.campaigns)
    assert all(pair.score.hypothesis != "same_campaign" for pair in result.pairs.values())


def test_corrupt_image_does_not_prevent_valid_sibling_processing(settings, tmp_path):
    from tests.adversarial.test_variants import creative

    source = tmp_path / "synthetic-input"
    good, bad = source / "good", source / "bad"
    good.mkdir(parents=True)
    bad.mkdir()
    creative().save(good / "reklama.png")
    (bad / "reklama.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 600)
    result = analyse([source], settings, only=["artifact_sha256", "vis"], use_cache=False, prefix="astra")
    assert "astra-good" in result.genomes
    assert any(g.status == "observed" for g in result.genomes["astra-good"].genes)
    assert "astra-bad" in result.genomes
    assert any(g.status in ("failed", "unavailable") for g in result.genomes["astra-bad"].genes)


def test_byte_duplicate_deduplicates_storage_and_links_without_crashing(settings, tmp_path):
    from tests.adversarial.test_variants import creative

    source = tmp_path / "synthetic-duplicates"
    for name in ("a", "b"):
        folder = source / name
        folder.mkdir(parents=True)
        creative().save(folder / "reklama.png")
    result = analyse([source], settings, only=["artifact_sha256"], use_cache=False, prefix="astra")
    assert result.clustering.membership["astra-a"] == result.clustering.membership["astra-b"]
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
