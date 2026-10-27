"""Every existing numeric week path remains discoverable without renaming it."""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import runpy

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/m2-journal.py"


@pytest.fixture
def journal():
    return runpy.run_path(str(SCRIPT))


def record(number, state):
    return {
        "id": f"00000000-0000-0000-0000-{number:012d}",
        "task": "synthetic week discovery",
        "before": "private synthetic baseline",
        "actions": "append and replay",
        "checks": "private state and pointer readback",
        "result": "synthetic result",
        "git_sha": None,
        "release_required": False,
        "state": state,
    }


def at(year):
    return datetime(year, 1, 4, tzinfo=UTC)


def state_hash(root):
    return hashlib.sha256((root / "current-state.md").read_bytes()).hexdigest()


def history(root):
    text = (root / "journal/INDEX.md").read_text(encoding="utf-8")
    return [
        line[3:].partition("](")[0]
        for line in text.partition("## История\n\n")[2].splitlines()
        if line.startswith("- [")
    ]


def assert_pointers(root, current, latest):
    text = (root / "journal/INDEX.md").read_text(encoding="utf-8")
    assert f"Текущая ISO-неделя UTC: [{current}]({current}.md)." in text
    assert f"[последняя сводка с результатами](../summaries/{latest}.md)" in text
    assert (root / "summaries" / (latest + ".md")).is_file()


@pytest.mark.parametrize("old_year,new_year", [(1, 2026), (9, 10), (99, 100), (999, 1000)])
@pytest.mark.parametrize("guarded", [False, True], ids=["without-CAS", "original-CAS"])
def test_old_writer_paths_remain_visible_to_exact_uuid_replay(journal, tmp_path, old_year, new_year, guarded):
    root = tmp_path / "M2"
    old = record(1, "older synthetic state")
    newer = record(2, "newer synthetic state")
    old_week, current_week = f"{old_year}-W01", f"{new_year}-W01"
    original_options = {"expected_state_sha256": "absent"} if guarded else {}
    journal["run"](root=root, record=old, at=at(old_year), **original_options)
    old_path = root / "journal" / (old_week + ".md")
    old_bytes = old_path.read_bytes()
    newer_options = {"expected_state_sha256": state_hash(root)} if guarded else {}
    journal["run"](root=root, record=newer, at=at(new_year), **newer_options)
    current_path = root / "journal" / (current_week + ".md")
    newer_bytes = current_path.read_bytes()
    state = (root / "current-state.md").read_bytes()

    report = journal["run"](root=root, record=old, at=at(new_year), **original_options)

    assert report["week"] == current_week and report["entries"] == 1
    assert (root / "current-state.md").read_bytes() == state
    assert old_path.read_bytes() == old_bytes and current_path.read_bytes() == newer_bytes
    assert [row["id"] for row in journal["records"](old_path)] == [old["id"]]
    assert [row["id"] for row in journal["records"](current_path)] == [newer["id"]]
    assert journal["week_for"](at(old_year)) == old_week
    assert not (root / "journal" / (f"{old_year:04d}-W01.md")).exists()
    assert history(root) == [old_week, current_week]
    assert_pointers(root, current_week, current_week)


@pytest.mark.parametrize("low,high", [(9, 10), (99, 100), (999, 1000)])
@pytest.mark.parametrize("reverse", [False, True], ids=["forward-creation", "reverse-creation"])
def test_history_and_latest_use_numeric_year_order(journal, tmp_path, low, high, reverse):
    root = tmp_path / "M2"
    years = (high, low) if reverse else (low, high)
    for year in years:
        journal["run"](root=root, record=record(year, f"synthetic state {year}"), at=at(year))
    paths = [root / "journal" / (f"{year}-W01.md") for year in (low, high)]
    originals = {path: path.read_bytes() for path in paths}
    state = (root / "current-state.md").read_bytes()

    report = journal["run"](root=root, at=at(2026))

    assert report["entries"] == 0
    assert history(root) == [f"{low}-W01", f"{high}-W01", "2026-W01"]
    assert_pointers(root, "2026-W01", f"{high}-W01")
    assert (root / "current-state.md").read_bytes() == state
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_numeric_alias_paths_remain_distinct(journal, tmp_path):
    root = tmp_path / "M2"
    journal["run"](root=root, at=at(2026))
    aliases = ["0009-W01", "009-W01", "09-W01", "9-W01"]
    originals = {}
    for alias in aliases:
        path = root / "journal" / (alias + ".md")
        path.write_text("# synthetic existing numeric alias\n", encoding="utf-8")
        originals[path] = path.read_bytes()

    journal["run"](root=root, at=at(2026))

    assert history(root) == [*aliases, "2026-W01"]
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert_pointers(root, "2026-W01", "2026-W01")


@pytest.mark.parametrize("stem", ["0000-W00", "0009-W99"])
def test_previous_numeric_filename_syntax_is_not_calendar_validated(journal, tmp_path, stem):
    root = tmp_path / "M2"
    journal["run"](root=root, at=at(2026))
    path = root / "journal" / (stem + ".md")
    path.write_text("# synthetic prior matching filename\n", encoding="utf-8")
    original = path.read_bytes()

    journal["run"](root=root, at=at(2026))

    assert history(root) == [stem, "2026-W01"]
    assert path.read_bytes() == original


def test_existing_dangling_week_symlink_still_fails_closed(journal, tmp_path):
    root = tmp_path / "M2"
    journal["run"](root=root, at=at(2026))
    path = root / "journal/0009-W01.md"
    path.symlink_to(tmp_path / "missing-journal.md")

    with pytest.raises(ValueError, match=r"^journal_symlink$"):
        journal["run"](root=root, at=at(2026))

    assert path.is_symlink()


@pytest.mark.parametrize("already_journaled", [False, True], ids=["before-journal", "after-journal"])
def test_partial_recovery_preserves_old_paths_and_numeric_latest(journal, tmp_path, already_journaled):
    root = tmp_path / "M2"
    old = record(1, "older synthetic state")
    accepted = record(2, "accepted synthetic state")
    journal["run"](root=root, record=old, at=at(9))
    old_path = root / "journal/9-W01.md"
    old_bytes = old_path.read_bytes()
    old_state = (root / "current-state.md").read_bytes()
    recovered_path = root / "journal/10-W01.md"
    before_journal = None
    if already_journaled:
        journal["run"](root=root, record=accepted, at=at(10))
        payload = journal["records"](recovered_path)[0]
        before_journal = recovered_path.read_bytes()
        (root / "current-state.md").write_bytes(old_state)
    else:
        payload = {**accepted, "at": at(10).isoformat()}
    pending = root / ".pending-record.json"
    pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    pending.chmod(0o600)

    journal["run"](root=root, at=at(2026))

    assert journal["records"](recovered_path) == [payload]
    if already_journaled:
        assert recovered_path.read_bytes() == before_journal
    assert old_path.read_bytes() == old_bytes
    assert not pending.exists() and not (root / "journal/0010-W01.md").exists()
    assert (root / "current-state.md").read_text(encoding="utf-8") == (
        "# M2 — текущее состояние\n\n" + accepted["state"] + "\n"
    )
    assert history(root) == ["9-W01", "10-W01", "2026-W01"]
    assert_pointers(root, "2026-W01", "10-W01")


@pytest.mark.parametrize("year", [2026, 9999])
@pytest.mark.parametrize("guarded", [False, True], ids=["without-CAS", "original-CAS"])
def test_four_digit_year_controls_keep_exact_replay_behavior(journal, tmp_path, year, guarded):
    root = tmp_path / "M2"
    old = record(1, "older synthetic state")
    newer = record(2, "newer synthetic state")
    options = {"expected_state_sha256": "absent"} if guarded else {}
    journal["run"](root=root, record=old, at=at(year), **options)
    newer_options = {"expected_state_sha256": state_hash(root)} if guarded else {}
    journal["run"](root=root, record=newer, at=at(year), **newer_options)
    path = root / "journal" / (f"{year}-W01.md")
    original = path.read_bytes()
    state = (root / "current-state.md").read_bytes()

    report = journal["run"](root=root, record=old, at=at(year), **options)

    assert report["entries"] == 2
    assert (root / "current-state.md").read_bytes() == state and path.read_bytes() == original
    assert history(root) == [f"{year}-W01"]
    assert_pointers(root, f"{year}-W01", f"{year}-W01")
