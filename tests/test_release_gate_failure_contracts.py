"""Exercise release-gate failures with inert callbacks and the real JSON validator.

Only a private copy of the gate runs. Callback commands and git are inert;
embedded stdin validation uses the real pytest interpreter. No project checks,
application tests or services run from the copied script.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest


_FIRST = ["scripts/check-automotive-catalog.py"]
_LATE = ["scripts/check-e2-branch-coverage.py"]
_VALID = '{"ok": true, "checks": [{"warnings": [], "missing_files": []}]}'

_CALLBACK = r"""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
gate = Path(os.environ["AUTOSTOP_MANAGER_DB"]).parent
kind = "validator" if args[:1] == ["-"] else "callback"
with Path(os.environ["REVIEW_GATE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps({"kind": kind, "args": args, "gate": str(gate)}) + "\n")
if kind == "validator":
    executable = os.environ["REVIEW_REAL_PYTHON"]
    os.execv(executable, [executable, *args])
if args == ["scripts/check-automotive-catalog.py"]:
    outside = os.environ.get("REVIEW_OUTSIDE_LINK")
    if outside:
        (gate / "outside-link").symlink_to(outside, target_is_directory=True)
    print(os.environ["REVIEW_FIRST_REPORT"])
else:
    print('{"ok": true}')
failure_prefix = json.loads(os.environ.get("REVIEW_FAILURE_PREFIX", "[]"))
if failure_prefix and args[:len(failure_prefix)] == failure_prefix:
    raise SystemExit(int(os.environ["REVIEW_FAILURE_CODE"]))
"""

_GIT = r"""
import json
import os
from pathlib import Path
import sys
with Path(os.environ["REVIEW_GATE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps({"kind": "git", "args": sys.argv[1:]}) + "\n")
"""

_FAIL_CLEANUP = r"""
import json
import os
from pathlib import Path
import sys
with Path(os.environ["REVIEW_GATE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps({"kind": "cleanup-failure", "args": sys.argv[1:]}) + "\n")
raise SystemExit(47)
"""


def _executable(path, source):
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(source), encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def inert_gate(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts/release-gates.sh"
    assert source.is_file(), f"Release gate missing: {source}"
    bash = shutil.which("bash")
    assert bash, "This shell regression requires Linux with bash and GNU utilities"
    project = tmp_path / "inert-project"
    script = project / "scripts/release-gates.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(source, script)
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    _executable(python, _CALLBACK)
    commands = tmp_path / "inert-commands"
    commands.mkdir()
    _executable(commands / "git", _GIT)
    log = tmp_path / "calls.jsonl"
    outside = tmp_path / "outside-gate"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve external synthetic data", encoding="utf-8")
    sentinel.chmod(0o400)
    outside.chmod(0o500)

    def records():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def run(*, report=_VALID, failure_prefix=(), failure_code=0, symlink=False, fail_cleanup=False):
        assert not log.exists(), "One gate process per fixture keeps ownership unambiguous"
        if fail_cleanup:
            _executable(commands / "find", _FAIL_CLEANUP)
        env = {
            "PATH": str(commands) + os.pathsep + os.defpath,
            "HOME": str(tmp_path),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "AUTOSTOP_MANAGER_ENV_FILE": os.devnull,
            "REVIEW_GATE_LOG": str(log),
            "REVIEW_REAL_PYTHON": sys.executable,
            "REVIEW_FIRST_REPORT": report,
            "REVIEW_FAILURE_PREFIX": json.dumps(list(failure_prefix)),
            "REVIEW_FAILURE_CODE": str(failure_code),
            "REVIEW_OUTSIDE_LINK": str(outside) if symlink else "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [bash, str(script)],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        calls = records()
        gates = {Path(call["gate"]) for call in calls if "gate" in call}
        assert len(gates) == 1, calls
        gate = gates.pop()
        return result, calls, gate, sentinel

    try:
        yield run
    finally:
        outside.chmod(0o700)
        sentinel.chmod(0o600)
        # Only the directory reported by this process's inert callbacks belongs
        # to this fixture. Normally the real gate already removed it. A deliberate
        # cleanup fault must not leave its private data behind after the test.
        for gate in {Path(call["gate"]) for call in records() if "gate" in call}:
            assert gate.parent == Path("/tmp") and gate.name.startswith("autostop-manager-release-gates.")
            assert not gate.is_symlink()
            if gate.exists():
                shutil.rmtree(gate)


def _callbacks(calls):
    return [call["args"] for call in calls if call["kind"] == "callback"]


def test_valid_reports_pass_and_cleanup_does_not_follow_internal_symlink(inert_gate):
    result, calls, gate, sentinel = inert_gate(symlink=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "release_gates_ok=true" in result.stdout
    assert any(call["kind"] == "validator" for call in calls)
    assert [call["args"] for call in calls if call["kind"] == "git"] == [
        ["diff", "--check"],
        ["diff", "--cached", "--check"],
    ]
    assert not gate.exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve external synthetic data"
    assert list(sentinel.parent.iterdir()) == [sentinel]
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o400
    assert stat.S_IMODE(sentinel.parent.stat().st_mode) == 0o500


@pytest.mark.parametrize(
    "report",
    [
        "{",
        '{"ok": false}',
        '{"ok": true, "checks": [{"warnings": ["synthetic warning"]}]}',
        '{"ok": true, "checks": {"catalog": {"missing_files": ["synthetic.md"]}}}',
    ],
    ids=["malformed-json", "not-ok", "nested-warnings", "nested-missing-files"],
)
def test_zero_exit_callback_cannot_pass_an_invalid_report(inert_gate, report):
    result, calls, gate, _sentinel = inert_gate(report=report)
    assert result.returncode != 0
    assert "release_gates_ok=true" not in result.stdout
    assert _callbacks(calls) == [_FIRST]
    assert any(call["kind"] == "validator" for call in calls)
    assert not any(call["kind"] == "git" for call in calls)
    assert not gate.exists()


@pytest.mark.parametrize(("failure_prefix", "failure_code"), [(_FIRST, 23), (_LATE, 29)], ids=["early", "late"])
def test_callback_failure_preserves_exit_stops_followups_and_cleans_up(inert_gate, failure_prefix, failure_code):
    result, calls, gate, _sentinel = inert_gate(failure_prefix=failure_prefix, failure_code=failure_code)
    assert result.returncode == failure_code, result.stdout + result.stderr
    assert "release_gates_ok=true" not in result.stdout
    callbacks = _callbacks(calls)
    assert callbacks[-1][: len(failure_prefix)] == failure_prefix
    if failure_prefix == _FIRST:
        assert callbacks == [_FIRST]
        assert not any(call["kind"] == "validator" for call in calls)
    else:
        assert ["-m", "coverage", "report", "--fail-under=82"] in callbacks
        assert any(call["kind"] == "validator" for call in calls)
    assert not any(call["kind"] == "git" for call in calls)
    assert not gate.exists()


def test_cleanup_failure_cannot_print_success_marker(inert_gate):
    result, calls, gate, sentinel = inert_gate(fail_cleanup=True)
    assert result.returncode != 0
    assert "release_gates_ok=true" not in result.stdout
    assert any(call["kind"] == "cleanup-failure" for call in calls)
    assert any(call["kind"] == "git" for call in calls)
    assert gate.is_dir()  # The fixture, not the faulting child, removes it finally.
    assert sentinel.read_text(encoding="utf-8") == "preserve external synthetic data"
