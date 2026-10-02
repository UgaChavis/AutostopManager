import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-e2-branch-coverage.py"
E2_FILES = runpy.run_path(str(SCRIPT))["E2_FILES"]


def _report(covered, total=100):
    return {
        "meta": {"branch_coverage": True},
        "totals": {"percent_covered": 99},
        "files": {name: {"summary": {"covered_branches": covered, "num_branches": total}} for name in E2_FILES},
    }


@pytest.mark.parametrize(("covered", "total", "code"), [(81, 100, 1), (82, 100, 0), (81999, 100000, 1)])
def test_e2_gate_uses_exact_branch_counts(tmp_path, covered, total, code):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(_report(covered, total)))
    result = subprocess.run([sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True, check=False)
    assert result.returncode == code
    assert "E2 branch coverage:" in result.stdout


def test_e2_gate_weights_files_by_branch_count(tmp_path):
    payload = _report(90)
    payload["files"][E2_FILES[0]]["summary"] = {"covered_branches": 700, "num_branches": 1000}
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(payload))
    result = subprocess.run([sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "73.33% (880/1200)" in result.stdout


@pytest.mark.parametrize("invalid", ["missing_file", "no_branches", "empty_counts", "negative_counts", "malformed"])
def test_e2_gate_rejects_incomplete_reports(tmp_path, invalid):
    payload = _report(90)
    if invalid == "missing_file":
        del payload["files"][E2_FILES[0]]
    elif invalid == "no_branches":
        payload["meta"]["branch_coverage"] = False
    elif invalid == "empty_counts":
        payload = _report(0, 0)
    elif invalid == "negative_counts":
        payload = _report(-1)
    report = tmp_path / "coverage.json"
    report.write_text("{" if invalid == "malformed" else json.dumps(payload))
    result = subprocess.run([sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "report invalid:" in result.stderr
