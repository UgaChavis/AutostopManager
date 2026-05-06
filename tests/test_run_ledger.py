from __future__ import annotations

from autostop_manager.storage import ManagerMemoryStore


def test_manager_run_ledger_records_events_and_finish_state(tmp_path):
    store = ManagerMemoryStore(tmp_path / "memory.sqlite3")

    started = store.start_manager_run(
        intent="board_cleanup",
        query="Приберись",
        dry_run=True,
        source="test",
        metadata={"scope": "active_board"},
    )
    assert started["ok"] is True

    event = store.record_manager_run_event(
        started["id"],
        event_type="planned_action",
        message="Update card summaries without moving cards",
        target_type="card",
        target_id="C-1",
        payload={"write": "description"},
    )
    assert event["ok"] is True

    finished = store.finish_manager_run(
        started["id"],
        status="completed",
        summary="Checked 1 card",
        verification={"cards_moved": 0},
    )
    assert finished["ok"] is True

    listed = store.list_manager_runs(limit=5, include_events=True)

    assert listed["ok"] is True
    assert listed["items"][0]["id"] == started["id"]
    assert listed["items"][0]["status"] == "completed"
    assert listed["items"][0]["dry_run"] is True
    assert listed["items"][0]["events"][0]["event_type"] == "planned_action"
    assert listed["items"][0]["verification"]["cards_moved"] == 0
