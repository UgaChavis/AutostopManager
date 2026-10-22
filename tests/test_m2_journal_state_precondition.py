from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_journal_state_precondition", ROOT / "scripts/m2-journal.py")
assert SPEC and SPEC.loader
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)

AT = datetime(2030, 1, 1, tzinfo=UTC)
HEADER = "# M2 — текущее состояние\n\n"
S0 = "Open technical question Q0.\n"
QA = "Pending independent check QA.\n"
QB = "Pending independent check QB.\n"
ID_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ID_B = "11111111-2222-4333-8444-555555555555"
ID_C = "22222222-3333-4444-8555-666666666666"


def record(record_id, state):
    return {
        "id": record_id,
        "task": "synthetic independent maintenance",
        "before": "synthetic baseline",
        "actions": "preserve open technical questions",
        "checks": "private helper fixture",
        "result": "synthetic source ready",
        "git_sha": None,
        "release_required": False,
        "state": state,
    }


def state_hash(root):
    return hashlib.sha256((root / "current-state.md").read_bytes()).hexdigest()


def files(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def entries(root):
    return [entry for path in sorted((root / "journal").glob("????-W??.md")) for entry in journal.records(path)]


def test_guarded_snapshots_reject_stale_state_and_accept_deliberate_rebase(tmp_path):
    root = tmp_path / "M2"
    root.mkdir()
    (root / "current-state.md").write_text(HEADER + S0, encoding="utf-8")
    baseline = state_hash(root)  # Both callers prepare their disjoint updates before either append.
    a = record(ID_A, S0 + QA)
    b = record(ID_B, S0 + QB)

    journal.run(root=root, record=a, at=AT, expected_state_sha256=baseline)
    after_a = files(root)
    with pytest.raises(ValueError, match=r"^journal_state_conflict$"):
        journal.run(root=root, record=b, at=AT, expected_state_sha256=baseline)

    assert files(root) == after_a
    assert entries(root) == [{**a, "at": AT.isoformat()}]
    assert not (root / ".pending-record.json").exists()
    c = record(ID_C, S0 + QA + QB)
    report = journal.run(root=root, record=c, at=AT, expected_state_sha256=state_hash(root))
    assert report["entries"] == 2
    assert (root / "current-state.md").read_text() == HEADER + S0 + QA + QB
    assert entries(root) == [{**a, "at": AT.isoformat()}, {**c, "at": AT.isoformat()}]


def test_old_token_exact_and_alias_replay_preserve_newer_state_and_uuid_conflict(tmp_path):
    root = tmp_path / "M2"
    a = record(ID_A, S0 + QA)
    journal.run(root=root, record=a, at=AT, expected_state_sha256="absent")
    c = record(ID_C, S0 + QA + QB)
    journal.run(root=root, record=c, at=AT, expected_state_sha256=state_hash(root))
    newer = files(root)

    for replay_id in (ID_A, ID_A.upper()):
        replay = journal.run(root=root, record={**a, "id": replay_id}, at=AT, expected_state_sha256="absent")
        assert replay["entries"] == 2
        assert files(root) == newer
    with pytest.raises(ValueError, match=r"^journal_record_conflict$"):
        journal.run(root=root, record={**a, "state": "changed body"}, at=AT, expected_state_sha256="absent")
    assert files(root) == newer


@pytest.mark.parametrize("replay_pending", [False, True])
def test_accepted_pending_recovers_before_new_precondition_or_exact_replay(tmp_path, replay_pending):
    root = tmp_path / "M2"
    root.mkdir()
    (root / "current-state.md").write_text(HEADER + S0, encoding="utf-8")
    old_hash = state_hash(root)
    accepted = record(ID_A, S0 + QA)
    payload = {**accepted, "at": AT.isoformat()}
    pending = root / ".pending-record.json"
    pending.write_text(json.dumps(payload), encoding="utf-8")

    if replay_pending:
        result = journal.run(root=root, record=accepted, at=AT, expected_state_sha256=old_hash)
        assert result["entries"] == 1
    else:
        with pytest.raises(ValueError, match=r"^journal_state_conflict$"):
            journal.run(root=root, record=record(ID_B, S0 + QB), at=AT, expected_state_sha256=old_hash)

    assert not pending.exists()
    assert entries(root) == [payload]
    assert (root / "current-state.md").read_text() == HEADER + S0 + QA


def test_absence_and_empty_file_are_distinct_and_other_read_errors_propagate(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    state = empty / "current-state.md"
    state.write_bytes(b"")
    a = record(ID_A, S0 + QA)
    with pytest.raises(ValueError, match=r"^journal_state_conflict$"):
        journal.run(root=empty, record=a, at=AT, expected_state_sha256="absent")
    assert state.read_bytes() == b"" and entries(empty) == []
    empty_hash = hashlib.sha256(b"").hexdigest()
    assert journal.run(root=empty, record=a, at=AT, expected_state_sha256=empty_hash)["entries"] == 1

    missing = tmp_path / "missing"
    b = record(ID_B, S0 + QB)
    with pytest.raises(ValueError, match=r"^journal_state_conflict$"):
        journal.run(root=missing, record=b, at=AT, expected_state_sha256=empty_hash)
    assert not (missing / "current-state.md").exists() and entries(missing) == []
    assert journal.run(root=missing, record=b, at=AT, expected_state_sha256="absent")["entries"] == 1

    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "current-state.md").mkdir()
    with pytest.raises(IsADirectoryError):
        journal.run(root=directory, record=a, at=AT, expected_state_sha256="absent")
    assert entries(directory) == [] and not (directory / ".pending-record.json").exists()


@pytest.mark.parametrize("token", [True, "A" * 64, "a" * 63, "a" * 64 + "\n"])
def test_invalid_precondition_cannot_create_root_or_recover_pending(tmp_path, token):
    absent = tmp_path / "absent" / "M2"
    a = record(ID_A, S0 + QA)
    with pytest.raises(ValueError, match=r"^journal_state_precondition_invalid$"):
        journal.run(root=absent, record=a, at=AT, expected_state_sha256=token)
    assert not absent.parent.exists()

    pending_root = tmp_path / "pending"
    pending_root.mkdir()
    (pending_root / ".pending-record.json").write_text(json.dumps({**a, "at": AT.isoformat()}))
    before = files(pending_root)
    with pytest.raises(ValueError, match=r"^journal_state_precondition_invalid$"):
        journal.run(root=pending_root, record=a, at=AT, expected_state_sha256=token)
    assert files(pending_root) == before


def test_precondition_without_record_is_rejected_before_creation(tmp_path):
    root = tmp_path / "absent" / "M2"
    with pytest.raises(ValueError, match=r"^journal_state_precondition_requires_record$"):
        journal.run(root=root, at=AT, expected_state_sha256="absent")
    assert not root.parent.exists()


@pytest.mark.parametrize("dangling", [False, True])
def test_precondition_rejects_state_symlink_before_absence_or_publication(tmp_path, dangling):
    root = tmp_path / "M2"
    root.mkdir()
    target = tmp_path / "synthetic-target"
    if not dangling:
        target.write_bytes(b"preserve private target")
    state = root / "current-state.md"
    state.symlink_to(target)

    with pytest.raises(ValueError, match=r"^journal_symlink$"):
        journal.run(root=root, record=record(ID_A, S0 + QA), at=AT, expected_state_sha256="absent")
    assert state.is_symlink() and entries(root) == []
    assert not (root / ".pending-record.json").exists()
    assert (not target.exists()) if dangling else target.read_bytes() == b"preserve private target"


def test_cli_documents_precondition_and_rejects_it_on_start_without_running(monkeypatch, capsys):
    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("CLI validation must stop before any journal root is used")

    # This is a live-root safety guard only; observable helper tests above use
    # the unmodified run directly, without API adapters or write monkeypatches.
    monkeypatch.setattr(journal, "run", forbidden_run)
    monkeypatch.setattr(sys, "argv", [str(SPEC.origin), "start", "--expected-state-sha256", "absent"])
    with pytest.raises(SystemExit) as invalid:
        journal.main()
    assert invalid.value.code == 2
    assert "requires append" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", [str(SPEC.origin), "--help"])
    with pytest.raises(SystemExit) as help_result:
        journal.main()
    assert help_result.value.code == 0
    assert "--expected-state-sha256" in capsys.readouterr().out
