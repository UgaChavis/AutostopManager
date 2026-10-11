from datetime import datetime, UTC
import importlib.util
import json
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_journal_summary", ROOT / "scripts/m2-journal.py")
assert SPEC and SPEC.loader
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)


def synthetic_record(git_sha=None):
    return {
        "id": str(UUID(int=1)),
        "task": "synthetic",
        "before": "synthetic",
        "actions": "synthetic",
        "checks": "synthetic",
        "result": "synthetic",
        "git_sha": git_sha,
        "release_required": False,
        "state": "synthetic source ready; release pending",
    }


@pytest.mark.parametrize(
    ("git_sha", "accepted"),
    [
        (None, True),
        ("0" * 40, True),
        ("0123456789abcdef" * 2 + "01234567", True),
        (int("1" * 40), False),
        (True, False),
        (1.0, False),
        ({"sha": "a" * 40}, False),
        (["a" * 40], False),
        ("", False),
        ("a" * 39, False),
        ("a" * 41, False),
        ("A" * 40, False),
        ("g" * 40, False),
        ("a" * 40 + "\n", False),
    ],
)
def test_sha_type_and_format_agree_with_documented_schema(git_sha, accepted):
    schema = json.loads((ROOT / "docs/agent/references/m2-journal-record.schema.json").read_text())
    record = synthetic_record(git_sha)
    assert Draft202012Validator(schema).is_valid(record) is accepted
    if accepted:
        journal.validate(record)
    else:
        with pytest.raises(ValueError, match=r"^journal_record_sha$"):
            journal.validate(record)


def test_invalid_append_does_not_create_journal_parent(tmp_path):
    root = tmp_path / "missing-parent" / "M2"
    with pytest.raises(ValueError, match=r"^journal_record_sha$"):
        journal.run(root, synthetic_record(int("1" * 40)), datetime(2026, 10, 1, tzinfo=UTC))
    assert not root.parent.exists()


def test_invalid_append_does_not_recover_or_change_existing_pending(tmp_path):
    root = tmp_path / "M2"
    root.mkdir()
    pending = root / ".pending-record.json"
    pending.write_text(json.dumps({**synthetic_record(), "at": "2026-10-01T00:00:00+00:00"}))
    (root / "current-state.md").write_text("existing technical state")
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(ValueError, match=r"^journal_record_sha$"):
        journal.run(root, synthetic_record("A" * 40), datetime(2026, 10, 1, tzinfo=UTC))
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


def test_numeric_pending_is_rejected_without_rewriting_history(tmp_path):
    pending = tmp_path / ".pending-record.json"
    pending.write_text(json.dumps({**synthetic_record(int("1" * 40)), "at": "2026-10-01T00:00:00+00:00"}))
    (tmp_path / "current-state.md").write_text("existing technical state")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(ValueError, match=r"^journal_record_sha$"):
        journal.recover(tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_weekly_summary_is_bounded_but_statistics_cover_all_records(tmp_path):
    at = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(14):
        record = {
            "id": str(UUID(int=i + 1)),
            "task": "synthetic",
            "before": "synthetic",
            "actions": "synthetic",
            "checks": "synthetic",
            "result": f"item-{i:02}: " + ("technical\n" * 140),
            "git_sha": "a" * 40,
            "release_required": i % 2 == 0,
            "state": "synthetic source ready; release pending",
        }
        report = journal.run(tmp_path, record, at)
    assert report == {"week": "2026-W40", "entries": 14, "commits": 1, "release_records": 7}
    summary = (tmp_path / "summaries/2026-W40.md").read_text()
    assert "14 результатов, 1 коммитов, 7 записей" in summary
    assert "item-00" not in summary and "item-13" in summary
    assert summary.count("\n- ") == 10
    assert len(summary) < 4000
    assert len(journal.records(tmp_path / "journal/2026-W40.md")) == 14


def test_summary_keeps_release_flags_as_history_after_completed_release(tmp_path):
    record = {
        "id": str(UUID(int=1)),
        "task": "synthetic source change",
        "before": "synthetic baseline",
        "actions": "synthetic commit",
        "checks": "synthetic checks passed",
        "result": "source ready",
        "git_sha": "a" * 40,
        "release_required": True,
        "state": "synthetic release pending",
    }
    at = datetime(2026, 10, 1, tzinfo=UTC)
    journal.run(tmp_path, record, at)
    completed = {
        **record,
        "id": str(UUID(int=2)),
        "release_required": False,
        "result": "installed revision independently verified",
        "state": "synthetic release completed; no pending release",
    }
    report = journal.run(tmp_path, completed, at)
    summary = (tmp_path / "summaries/2026-W40.md").read_text()
    assert report["release_records"] == 1
    assert "1 записей с отметкой о необходимости выпуска" in summary
    assert "требуют выпуска" not in summary
    assert completed["state"] in (tmp_path / "current-state.md").read_text()
