"""CLI and packaged UI contracts, with no server sockets or model downloads."""

from __future__ import annotations

import json
import warnings
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hydra import events
from hydra.api import create_app
from hydra.db import Writer, connect


@pytest.fixture
def cli(settings, monkeypatch):
    from hydra import cli as module
    from hydra import config

    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    return CliRunner(), module.app


@pytest.mark.parametrize("command", [
    ["serve"], ["import"], ["status"], ["verify"], ["analyse"], ["assign"], ["assign-batch"],
    ["export-neo4j"], ["export-dataset"], ["train"], ["corpora"],
    ["scheduler", "status"], ["scheduler", "watch"], ["scheduler", "run"],
])
def test_cli_commands_have_working_help(cli, command):
    runner, app = cli
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_cli_import_status_verify_and_detect_tampering(cli, settings, tmp_path):
    """The public command reports corruption through both its JSON and nonzero exit code."""
    from hydra.storage import ContentStore

    runner, app = cli
    source = tmp_path / "synthetic-chat.txt"
    source.write_text("Synthetic chat: identifier ALPHA-801 and an invented campaign.", encoding="utf-8")
    imported = runner.invoke(app, ["import", str(source)])
    assert imported.exit_code == 0, imported.output
    job = json.loads(imported.stdout)
    assert job["status"] == "done"
    counts = runner.invoke(app, ["status"])
    assert counts.exit_code == 0, counts.output
    assert json.loads(counts.stdout)["artifacts"] == {"text/plain": 1}
    verified = runner.invoke(app, ["verify"])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["audit_ok"] is True
    store = ContentStore(settings.cas_dir)
    digest = next(store.iter_digests())
    path = store.safe_path(digest)
    path.chmod(0o600)
    path.write_bytes(b"deliberately corrupted synthetic evidence")
    failed = runner.invoke(app, ["verify"])
    assert failed.exit_code == 1, failed.output
    assert json.loads(failed.stdout)["cas_mismatches"] == [digest]


def test_cli_analyse_json_and_export_round_trip(cli, settings, tmp_path):
    runner, app = cli
    folder = tmp_path / "captures"
    for i in range(3):
        incident = folder / f"case-{i}"
        incident.mkdir(parents=True)
        (incident / "landing.html").write_text(
            f'<html><title>Test {i}</title><form action="https://operator-{i}.example/submit">'
            f'<input name="lead" value="capture-{i}"></form></html>', encoding="utf-8")
    analysed = runner.invoke(app, ["analyse", str(folder), "--only", "inf_dom", "--json"])
    assert analysed.exit_code == 0, analysed.output
    output = json.loads(analysed.stdout)
    assert output["incidents"] == 3
    assert output["pairs_scored"] == 3
    target = tmp_path / "neo4j"
    exported = runner.invoke(app, ["export-neo4j", str(target), "--run", output["run_id"]])
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["incidents"] == 3
    assert (target / "import.cypher").is_file()
    verified = runner.invoke(app, ["verify"])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.stdout)["audit_ok"] is True


def test_cli_rejects_missing_analysis_input(cli):
    runner, app = cli
    result = runner.invoke(app, ["analyse", "--json"])
    assert result.exit_code == 2
    assert "give dataset paths" in result.output


def test_cli_assignment_batch_and_dataset_use_a_fixed_base_run(cli, tmp_path):
    """Exercise the jury CLI chain, including independent per-item rows and both export formats."""
    import csv

    runner, app = cli
    base = tmp_path / "known.html"
    base.write_text('<html><body><form action="https://synthetic.example/lead">'
                    '<input name="account" value="synthetic-801"></form></body></html>')
    analysed = runner.invoke(app, ["analyse", str(base), "--only", "inf_dom", "--json"])
    assert analysed.exit_code == 0, analysed.output
    base_run = json.loads(analysed.stdout)["run_id"]
    assigned = runner.invoke(app, ["assign", str(base), "--run", base_run, "--only", "inf_dom"])
    assert assigned.exit_code == 0, assigned.output
    # merge-batch3 (analyse_input): the assignment is nested under "assignment" next to the input report.
    assert json.loads(assigned.stdout)["assignment"]["reason_pl"]
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "=1+1.html").write_bytes(base.read_bytes())
    (incoming / "other.html").write_text('<html><p>Independent synthetic material</p></html>')
    out = tmp_path / "assignments"
    batch = runner.invoke(app, ["assign-batch", str(incoming), "--run", base_run, "--out", str(out)])
    assert batch.exit_code == 0, batch.output
    rows = json.loads((out / "assignments.json").read_text())
    assert len(rows) == 2
    assert all(row["base_run"] == base_run for row in rows)
    assert all(row["decision"] in {"assign", "abstain", "new_campaign"} for row in rows)
    assert len({row["assign_run"] for row in rows}) == 2
    with (out / "assignments.csv").open(newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    assert any(row["item"] == "'=1+1.html" for row in csv_rows)
    assert {row["base_run"] for row in csv_rows} == {base_run}
    dataset = tmp_path / "dataset"
    exported = runner.invoke(app, ["export-dataset", base_run, str(dataset)])
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["run_id"] == base_run
    assert (dataset / "manifest.json").is_file()
    counts = runner.invoke(app, ["corpora"])
    assert counts.exit_code == 0 and json.loads(counts.stdout), counts.output


def test_cli_scheduler_watch_and_offline_run_are_available(cli):
    runner, app = cli
    added = runner.invoke(app, ["scheduler", "watch", "--keyword", "synthetic-query"])
    assert added.exit_code == 0, added.output
    assert "synthetic-query" in added.output
    status = runner.invoke(app, ["scheduler", "status"])
    assert status.exit_code == 0, status.output
    run = runner.invoke(app, ["scheduler", "run"])
    assert run.exit_code == 0, run.output
    assert isinstance(json.loads(run.stdout), dict)


def test_audit_hash_chain_detects_modified_event(settings):
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda conn: events.append(conn, "SYNTHETIC_AUDIT", {"value": "original"}))
        writer.run(lambda conn: conn.execute("UPDATE events SET payload=?", ('{"value":"changed"}',)))
    finally:
        writer.close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        ok, count, bad = events.verify_chain(conn)
    assert not ok and count >= 1 and bad is not None


def test_packaged_spa_deep_link_does_not_shadow_api(settings, tmp_path):
    """Exercise the same static mount used by Docker without running a browser/server."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>SYNTHETIC_UI_SHELL</body></html>")
    local = settings.model_copy(update={"ui_dist": dist})
    with TestClient(create_app(local), raise_server_exceptions=False) as client:
        for path in ("/", "/campaigns/example", "/settings"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "SYNTHETIC_UI_SHELL" in response.text
        for path in ("/health", "/api/health"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
        missing = client.get("/v1/no-such-api-route")
        assert missing.status_code == 404
        assert "SYNTHETIC_UI_SHELL" not in missing.text


def test_published_api_schema_has_no_duplicate_operation_ids(settings):
    """Two handlers currently publish the same campaign lifecycle GET operation."""
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        schema = create_app(settings).openapi()
    assert "/v1/campaigns/{campaign_id}/lifecycle" in schema["paths"]
    duplicates = [str(item.message) for item in seen if "Duplicate Operation ID" in str(item.message)]
    assert not duplicates, duplicates
