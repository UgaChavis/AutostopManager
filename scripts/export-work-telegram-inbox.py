#!/usr/bin/env python3
"""Export exact legacy event locators before an explicitly authorized cutover."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autostop_manager.telegram_bridge import (  # noqa: E402
    BridgeError,
    INBOUND_EVENT_ID_PATTERN,
    WORK_SOCKET_PATH,
    send_local_request,
)


def collect_references(socket_path: Path = WORK_SOCKET_PATH) -> dict[str, Any]:
    before = send_local_request(socket_path, {"operation": "monitor_status"})
    listed = send_local_request(socket_path, {"operation": "monitor_events"})
    if before.get("ok") is not True or listed.get("ok") is not True:
        raise BridgeError("legacy_export_monitor_unavailable")
    rows = listed.get("events")
    if not isinstance(rows, list) or len(rows) > 32:
        raise BridgeError("legacy_export_events_invalid")
    references = []
    for row in rows:
        if not isinstance(row, dict):
            raise BridgeError("legacy_export_events_invalid")
        event_id = row.get("event_id")
        received_at = row.get("received_at")
        state = row.get("state")
        if (
            not isinstance(event_id, str)
            or INBOUND_EVENT_ID_PATTERN.fullmatch(event_id) is None
            or not isinstance(received_at, str)
            or not isinstance(state, str)
            or state not in {"open", "reply_verified", "no_reply_needed"}
        ):
            raise BridgeError("legacy_export_events_invalid")
        timestamp = datetime.fromisoformat(received_at)
        if timestamp.tzinfo is None:
            raise BridgeError("legacy_export_events_invalid")
        target = send_local_request(socket_path, {"operation": "monitor_target", "event_id": event_id})
        peer = target.get("target")
        if (
            target.get("ok") is not True
            or target.get("event_id") != event_id
            or not isinstance(peer, dict)
            or peer.get("kind") != "private"
            or type(peer.get("id")) is not int
            or peer["id"] <= 0
            or type(target.get("reply_to_message_id")) is not int
            or target["reply_to_message_id"] <= 0
        ):
            raise BridgeError("legacy_export_target_invalid")
        references.append(
            {
                "event_id": event_id,
                "peer_id": peer["id"],
                "message_id": target["reply_to_message_id"],
                "received_at": timestamp.astimezone(UTC).isoformat(),
                "state": state,
            }
        )
    after = send_local_request(socket_path, {"operation": "monitor_status"})
    final_events = send_local_request(socket_path, {"operation": "monitor_events"})
    initial = before.get("monitor", before)
    final = after.get("monitor", after)
    if (
        after.get("ok") is not True
        or not isinstance(initial, dict)
        or not isinstance(final, dict)
        or not isinstance(initial.get("started_at"), str)
        or not initial["started_at"]
        or initial.get("started_at") != final.get("started_at")
        or final_events.get("ok") is not True
        or final_events.get("events") != rows
    ):
        raise BridgeError("legacy_export_monitor_changed")
    return {"events": references}


def write_private_export(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute() or path.parent.is_symlink() or path.parent.resolve() != path.parent:
        raise BridgeError("legacy_export_path_invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise BridgeError("legacy_export_permissions_invalid")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("run_as_root_required=true", file=sys.stderr)
        return 1
    try:
        payload = collect_references()
        write_private_export(args.output, payload)
    except (BridgeError, OSError, ValueError, TypeError):
        print("legacy_export_failed=true", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "exported_events": len(payload["events"]), "automatic_replay": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
