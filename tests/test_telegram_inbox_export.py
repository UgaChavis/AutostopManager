"""Exercise legacy cutover locators without retaining or contacting customers."""

import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("telegram_inbox_export", ROOT / "scripts/export-work-telegram-inbox.py")
assert spec is not None and spec.loader is not None
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def test_export_whitelists_exact_locators_and_never_copies_content(monkeypatch, tmp_path):
    def request(_socket, body):
        if body["operation"] == "monitor_status":
            return {"ok": True, "monitor": {"started_at": "epoch"}}
        if body["operation"] == "monitor_events":
            return {
                "ok": True,
                "events": [{"event_id": "inbound-1", "received_at": "2026-10-12T00:00:00+00:00", "state": "open"}],
            }
        assert body == {"operation": "monitor_target", "event_id": "inbound-1"}
        return {
            "ok": True,
            "event_id": "inbound-1",
            "target": {"id": 1, "kind": "private", "title": "must-not-be-copied"},
            "reply_to_message_id": 2,
            "text": "must-not-be-copied",
        }

    monkeypatch.setattr(export, "send_local_request", request)
    payload = export.collect_references()
    destination = tmp_path / "private/events.json"
    export.write_private_export(destination, payload)
    text = destination.read_text()
    assert "must-not-be-copied" not in text
    assert json.loads(text)["events"][0]["peer_id"] == 1
    assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        export.write_private_export(destination, payload)


def test_export_refuses_monitor_restart(monkeypatch):
    values = iter(
        [
            {"ok": True, "monitor": {"started_at": "before"}},
            {"ok": True, "events": []},
            {"ok": True, "monitor": {"started_at": "after"}},
            {"ok": True, "events": []},
        ]
    )
    monkeypatch.setattr(export, "send_local_request", lambda *_args: next(values))
    with pytest.raises(export.BridgeError, match="legacy_export_monitor_changed"):
        export.collect_references()


def test_export_refuses_arrival_during_capture(monkeypatch):
    values = iter(
        [
            {"ok": True, "monitor": {"started_at": "epoch"}},
            {"ok": True, "events": []},
            {"ok": True, "monitor": {"started_at": "epoch"}},
            {"ok": True, "events": [{"event_id": "inbound-2", "state": "open"}]},
        ]
    )
    monkeypatch.setattr(export, "send_local_request", lambda *_args: next(values))
    with pytest.raises(export.BridgeError, match="legacy_export_monitor_changed"):
        export.collect_references()


def test_export_fails_closed_when_exact_target_is_unavailable(monkeypatch):
    values = iter(
        [
            {"ok": True, "monitor": {"started_at": "epoch"}},
            {"ok": True, "events": [{"event_id": "inbound-1", "received_at": "2026-10-12T00:00:00Z", "state": "open"}]},
            {"ok": False, "error": "inbound_event_unknown"},
        ]
    )
    monkeypatch.setattr(export, "send_local_request", lambda *_args: next(values))
    with pytest.raises(export.BridgeError, match="legacy_export_target_invalid"):
        export.collect_references()
