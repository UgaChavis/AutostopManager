from datetime import datetime, UTC
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("m2_journal_idempotency", ROOT / "scripts/m2-journal.py")
assert SPEC and SPEC.loader
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)

RECORD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ALIASES = (RECORD_ID.upper(), RECORD_ID.replace("-", ""), "{" + RECORD_ID + "}", "urn:uuid:" + RECORD_ID)
ID_PAIRS = [(RECORD_ID, alias) for alias in ALIASES] + [(alias, RECORD_ID) for alias in ALIASES]


def synthetic_record(record_id):
    return {
        "id": record_id,
        "task": "synthetic task",
        "before": "synthetic baseline",
        "actions": "synthetic change",
        "checks": "synthetic check",
        "result": "synthetic result",
        "git_sha": None,
        "release_required": False,
        "state": "older synthetic state",
    }


@pytest.mark.parametrize(("stored_id", "replay_id"), ID_PAIRS)
def test_uuid_alias_replay_preserves_newer_state_and_history(tmp_path, stored_id, replay_id):
    old = synthetic_record(stored_id)
    journal.run(tmp_path, old, datetime(2026, 12, 31, tzinfo=UTC))
    current = {**old, "id": "11111111-2222-4333-8444-555555555555", "state": "newer synthetic state"}
    at = datetime(2027, 1, 4, tzinfo=UTC)
    journal.run(tmp_path, current, at)
    state = (tmp_path / "current-state.md").read_bytes()
    old_week = (tmp_path / "journal/2026-W53.md").read_bytes()
    current_week = (tmp_path / "journal/2027-W01.md").read_bytes()

    report = journal.run(tmp_path, {**old, "id": replay_id}, at)

    assert report["entries"] == 1
    assert (tmp_path / "current-state.md").read_bytes() == state
    assert (tmp_path / "journal/2026-W53.md").read_bytes() == old_week
    assert (tmp_path / "journal/2027-W01.md").read_bytes() == current_week


@pytest.mark.parametrize(("stored_id", "replay_id"), ID_PAIRS)
def test_uuid_alias_with_changed_body_conflicts(tmp_path, stored_id, replay_id):
    old = synthetic_record(stored_id)
    at = datetime(2026, 10, 21, tzinfo=UTC)
    journal.run(tmp_path, old, at)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match=r"^journal_record_conflict$"):
        journal.run(tmp_path, {**old, "id": replay_id, "state": "different synthetic state"}, at)

    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(("stored_id", "pending_id"), ID_PAIRS)
def test_pending_uuid_alias_recovers_without_duplicate(tmp_path, stored_id, pending_id):
    old = synthetic_record(stored_id)
    at = datetime(2026, 10, 21, tzinfo=UTC)
    journal.run(tmp_path, old, at)
    path = tmp_path / "journal/2026-W43.md"
    before = path.read_bytes()
    (tmp_path / "current-state.md").write_text("synthetic incomplete state")
    pending = tmp_path / ".pending-record.json"
    pending.write_text(json.dumps({**old, "id": pending_id, "at": at.isoformat()}))

    report = journal.run(tmp_path, at=at)

    assert report["entries"] == 1
    assert path.read_bytes() == before
    assert old["state"] in (tmp_path / "current-state.md").read_text()
    assert not pending.exists()


@pytest.mark.parametrize(("stored_id", "pending_id"), ID_PAIRS)
def test_pending_uuid_alias_with_changed_body_conflicts(tmp_path, stored_id, pending_id):
    old = synthetic_record(stored_id)
    at = datetime(2026, 10, 21, tzinfo=UTC)
    journal.run(tmp_path, old, at)
    pending = tmp_path / ".pending-record.json"
    pending.write_text(json.dumps({**old, "id": pending_id, "state": "different state", "at": at.isoformat()}))
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match=r"^journal_record_conflict$"):
        journal.run(tmp_path, at=at)

    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
