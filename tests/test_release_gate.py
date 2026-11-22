"""S00 release gate: scripts/e2e_all.sh profiles and scripts/skip_report.py (no product code, no network)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
STEPS = ["check", "e2e", "ui-build", "ui", "sample", "security", "adversarial", "pipeline_audit"]


def _skip_report():
    spec = importlib.util.spec_from_file_location("skip_report", REPO / "scripts" / "skip_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["skip_report"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_all(tmp_path: Path, *args: str) -> tuple[int, str, dict]:
    if shutil.which("bash") is None:
        pytest.skip("bash missing")
    skips = [a for s in STEPS for a in ("--skip", s)]
    log = tmp_path / "log"
    proc = subprocess.run(["bash", str(REPO / "scripts" / "e2e_all.sh"), *args, *skips],
                          env={**os.environ, "E2E_ALL_LOG": str(log)},
                          cwd=REPO, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, json.loads((log / "summary.json").read_text())


def test_dev_profile_keeps_pass_when_steps_are_skipped(tmp_path):
    rc, out, summary = _run_all(tmp_path)
    assert rc == 0, out
    assert "OVERALL: PASS (dev profile: 8 SKIP" in out
    assert summary["profile"] == "dev" and summary["overall"] == "PASS"
    assert [s["result"] for s in summary["steps"]] == ["SKIP"] * 8


def test_release_profile_reports_partial_for_skipped_required_steps(tmp_path):
    rc, out, summary = _run_all(tmp_path, "--release")
    assert rc == 3, out
    assert "OVERALL: PARTIAL" in out
    assert summary["overall"] == "PARTIAL" and summary["required"] == STEPS
    assert {s["step"] for s in summary["steps"]} == set(STEPS)


def test_count_of_parses_pytest_and_playwright_summaries(tmp_path):
    script = ('eval "$(sed -n "/^count_of()/,/^}/p" scripts/e2e_all.sh)"; '
              'count_of "10 passed, 1 skipped, 2 xfailed in 3.1s; 4 passed; 1 flaky" xfailed; '
              'count_of "4 passed; 1 flaky" flaky; count_of "3 passed in 1s" xfailed')
    out = subprocess.run(["bash", "-c", script], cwd=REPO, capture_output=True, text=True, timeout=30).stdout.split()
    assert out == ["2", "1", "0"]


def test_skip_report_finds_markers_with_cr_and_owner(tmp_path):
    sr = _skip_report()
    entries = sr.collect()
    assert entries, "the suite declares skips (tools/models/sample gates)"
    for e in entries:
        assert e.category and e.owner, e
    defects = [e for e in entries if e.category.startswith("known defect")]
    assert all(e.crs for e in defects), [e for e in defects if not e.crs]
    out = tmp_path / "SKIP_XFAIL.md"
    assert sr.main(["--out", str(out), "--check"]) == 0
    text = out.read_text()
    assert "## Known defects" in text and "## All declarations" in text


def test_skip_report_scans_synthetic_markers(tmp_path):
    sr = _skip_report()
    src = tmp_path / "tests" / "core" / "test_x.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "import pytest\n"
        "needs_tool = pytest.mark.skipif(True, reason='tool missing')\n"
        "@needs_tool\n"
        "def test_a():\n    pass\n"
        "@pytest.mark.xfail(strict=True, reason='CR-CORE-99: wrong merge')\n"
        "def test_b():\n    pytest.skip('creative not here')\n"
    )
    old = sr.REPO
    sr.REPO = tmp_path
    try:
        entries = sr.scan_py(src)
    finally:
        sr.REPO = old
    kinds = sorted((e.kind, e.scope) for e in entries)
    assert kinds == [("runtime-skip", "test_b"), ("skipif", "needs_tool -> test_a"), ("xfail", "test_b")]
    xf = next(e for e in entries if e.kind == "xfail")
    assert "strict=True" in xf.condition and xf.reason.startswith("CR-CORE-99")
    assert sr.CR_RE.findall(xf.reason) == ["CR-CORE-99"]


def test_skip_report_parses_junit(tmp_path):
    sr = _skip_report()
    xml = tmp_path / "j.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="a" name="t1"><skipped type="pytest.skip" '
                   'message="ffmpeg missing"/></testcase><testcase classname="a" name="t2"><skipped '
                   'type="pytest.xfail" message="CR-E2E-2"/></testcase><testcase classname="a" name="t3"/>'
                   '</testsuite></testsuites>')
    got = [(o.test, o.outcome, o.reason) for o in sr.parse_junit([xml])]
    assert got == [("a::t1", "skipped", "ffmpeg missing"), ("a::t2", "xfailed", "CR-E2E-2")]
