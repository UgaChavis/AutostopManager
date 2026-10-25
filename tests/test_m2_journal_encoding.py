"""M2 text persistence has one UTF-8 format across process locales."""

from __future__ import annotations

from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/m2-journal.py"
AT = datetime(2026, 10, 25, tzinfo=UTC)
CLI = r"""
from datetime import datetime, UTC
from functools import partial
import locale
import os
from pathlib import Path
import runpy
import sys

script, role, sandbox, operation, record, token = sys.argv[1:]
role, sandbox = Path(role).resolve(), Path(sandbox).resolve()
assert role.is_relative_to(sandbox)
if sys.flags.utf8_mode == 0:
    assert locale.getpreferredencoding(False).upper() in {"ANSI_X3.4-1968", "ASCII", "US-ASCII"}
module = runpy.run_path(script)
actual_run, main = module["run"], module["main"]
# CLI has no root flag; inject only the explicit synthetic root and clock.
main.__globals__["run"] = partial(actual_run, root=role, at=datetime(2026, 10, 25, tzinfo=UTC))

def protected(path):
    if isinstance(path, int):
        path = os.readlink(f"/proc/self/fd/{path}")
    return Path(path).absolute().is_relative_to(Path("/var/lib/autostop-manager/roles"))

def guard(event, args):
    if event in {"open", "os.mkdir", "os.remove", "os.rmdir"} and protected(args[0]):
        raise AssertionError("test cannot access the installed role journal")
    if event == "os.rename" and (protected(args[0]) or protected(args[1])):
        raise AssertionError("test cannot replace installed role files")
    if event in {"socket.__new__", "socket.connect", "sqlite3.connect", "subprocess.Popen", "os.system", "os.fork"}:
        raise AssertionError("journal test attempted external activity")

sys.addaudithook(guard)
sys.argv = [script, operation]
if record != "-":
    sys.argv += ["--record", record]
if token != "-":
    sys.argv += ["--expected-state-sha256", token]
main()
"""


def record(number=1):
    return {
        "id": f"00000000-0000-0000-0000-{number:012d}",
        "task": "Синтетическая задача",
        "before": "Тестовое исходное состояние",
        "actions": "Проверка файлов журнала",
        "checks": "Синтетический результат",
        "result": "Кодировка сохранена — résumé 🚗",
        "git_sha": None,
        "release_required": False,
        "state": f"Синтетическое состояние {number}: résumé 🚗",
    }


def input_file(tmp_path, value, *, escaped=False):
    path = tmp_path / f"input-{value['id']}.json"
    path.write_text(json.dumps(value, ensure_ascii=escaped), encoding="utf-8")
    return path


def invoke(tmp_path, *, role=None, operation="append", supplied=None, token=None, utf8=0):
    role = role or tmp_path / "role"
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            f"utf8={utf8}",
            "-c",
            CLI,
            str(SCRIPT),
            str(role),
            str(tmp_path),
            operation,
            str(supplied) if supplied is not None else "-",
            token or "-",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


def assert_success(result, entries):
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "week": "2026-W43",
        "entries": entries,
        "commits": 0,
        "release_records": 0,
    }


def snapshot(role):
    return {str(p.relative_to(role)): p.read_bytes() for p in role.rglob("*") if p.is_file()}


def seed(role, value):
    module = runpy.run_path(str(SCRIPT))
    module["run"](root=role, record=value, at=AT)
    return module


def test_m2_start_writes_its_russian_headers_in_ascii_process(tmp_path):
    assert_success(invoke(tmp_path, operation="start"), 0)
    state = (tmp_path / "role/current-state.md").read_bytes().decode("utf-8")
    assert state.startswith("# M2 — текущее состояние")
    assert (tmp_path / "role/journal/INDEX.md").read_bytes().decode("utf-8").startswith("# M2 — индекс")


@pytest.mark.parametrize("escaped", [False, True])
def test_m2_ascii_process_accepts_utf8_or_escaped_json_and_writes_unicode(tmp_path, escaped):
    value = record()
    supplied = input_file(tmp_path, value, escaped=escaped)
    assert_success(invoke(tmp_path, supplied=supplied), 1)
    role = tmp_path / "role"
    assert (role / "current-state.md").read_bytes() == ("# M2 — текущее состояние\n\n" + value["state"] + "\n").encode()
    assert value["result"] in (role / "journal/2026-W43.md").read_bytes().decode("utf-8")
    assert not (role / ".pending-record.json").exists()


def test_m2_ascii_process_replays_existing_utf8_history_without_replacing_state(tmp_path):
    role, value = tmp_path / "role", record()
    seed(role, value)
    before = snapshot(role)
    # Escaped input isolates reading the already persisted Unicode files.
    supplied = input_file(tmp_path, value, escaped=True)
    assert_success(invoke(tmp_path, supplied=supplied, token="absent"), 1)
    assert snapshot(role) == before


@pytest.mark.parametrize("escaped_pending", [False, True])
def test_m2_ascii_process_recovers_pending_into_existing_utf8_history(tmp_path, escaped_pending):
    role = tmp_path / "role"
    seed(role, record(1))
    pending = {**record(2), "at": AT.isoformat()}
    (role / ".pending-record.json").write_text(json.dumps(pending, ensure_ascii=escaped_pending), encoding="utf-8")
    assert_success(invoke(tmp_path, operation="start"), 2)
    assert not (role / ".pending-record.json").exists()
    assert record(2)["state"] in (role / "current-state.md").read_bytes().decode("utf-8")
    module = runpy.run_path(str(SCRIPT))
    stored = module["records"](role / "journal/2026-W43.md")
    assert [entry["id"] for entry in stored] == [record(1)["id"], record(2)["id"]]


def test_m2_ascii_process_keeps_stale_state_precondition_and_files(tmp_path):
    role = tmp_path / "role"
    seed(role, record(1))
    before = snapshot(role)
    supplied = input_file(tmp_path, record(2), escaped=True)
    result = invoke(tmp_path, supplied=supplied, token=hashlib.sha256(b"stale").hexdigest())
    assert result.returncode != 0 and "journal_state_conflict" in result.stderr
    assert snapshot(role) == before


def test_m2_persisted_utf8_bytes_are_identical_in_ascii_and_utf8_processes(tmp_path):
    supplied = input_file(tmp_path, record())
    ascii_role, utf8_role = tmp_path / "ascii-role", tmp_path / "utf8-role"
    assert_success(invoke(tmp_path, role=ascii_role, supplied=supplied, utf8=0), 1)
    assert_success(invoke(tmp_path, role=utf8_role, supplied=supplied, utf8=1), 1)
    assert snapshot(ascii_role) == snapshot(utf8_role)


def test_m2_existing_utf8_process_keeps_accepting_unicode_input(tmp_path):
    assert_success(invoke(tmp_path, supplied=input_file(tmp_path, record()), utf8=1), 1)
