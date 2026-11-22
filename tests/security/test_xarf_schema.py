"""Formal XARF check: explicit missing-resource skip, never a fabricated local subset."""

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from hydra.db import Writer
from hydra.export_abuse import abuse_package
from tests.security.helpers import seed

SCHEMAS = Path(__file__).parent / "schemas"


@pytest.mark.parametrize("kind", ["fraud", "phishing"])
def test_xarf_against_pinned_published_schema(settings, kind):
    manifest_path = SCHEMAS / "manifest.json"
    if not manifest_path.is_file():
        pytest.skip("BLOCKED: published XARF schema/licence unavailable; see security/schemas/README.md")
    jsonschema = pytest.importorskip("jsonschema", reason="BLOCKED: JSON Schema validator is not installed")
    from referencing import Registry, Resource

    manifest = json.loads(manifest_path.read_text())
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["revision"])

    def checked_file(name, digest):
        path = (SCHEMAS / name).resolve()
        assert path.is_relative_to(SCHEMAS.resolve()), "Schema manifest escapes its fixture directory"
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        return raw

    checked_file(manifest["license_file"], manifest["license_sha256"])
    registry = Registry()  # No retrieve callback: unresolved remote refs are errors.
    documents = {}
    for record in manifest["documents"]:
        assert manifest["revision"] in record["url"], "Schema URL must name the pinned upstream revision"
        document = json.loads(checked_file(record["file"], record["sha256"]))
        documents[record["uri"]] = document
        registry = registry.with_resource(record["uri"], Resource.from_contents(document))
    root = documents[manifest["roots"][kind]]
    validator_cls = jsonschema.validators.validator_for(root)
    validator_cls.check_schema(root)
    validator = validator_cls(root, registry=registry, format_checker=jsonschema.FormatChecker())
    campaign, run, _ = seed(settings)
    if kind == "phishing":
        campaign.scheme = "phishing"
        writer = Writer(settings.db_path)
        try:
            writer.run(lambda conn: conn.execute("UPDATE campaigns SET detail=? WHERE id=? AND run_id=?",
                                                (campaign.model_dump_json(), campaign.id, run)))
        finally:
            writer.close()
    raw, _ = abuse_package(settings, campaign.id, run)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        report = json.loads(archive.read("reports/campaign.example.xarf.json"))
    assert report["type"] == kind
    validator.validate(report)
