from datetime import datetime, UTC
import importlib.util
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_journal_summary", ROOT / "scripts/m2-journal.py")
assert SPEC and SPEC.loader
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)


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
