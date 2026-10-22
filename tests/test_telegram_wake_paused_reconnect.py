"""In-memory reconnect diagnostics; never claim work or open transports."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
from telegram_wake_fakes import THREAD, until

from autostop_manager import telegram_wake as wake


@pytest.mark.parametrize(
    "blocker",
    [
        None,
        "intake_only",
        "execution_only",
        "review_alias_zero",
        "other_error",
        "execution_error",
        "app_disconnected",
        "not_ready",
        "blocked",
        "recovery_error",
        "recovery_state",
        "rpc_error",
        "outcome_unknown",
        "active_dispatcher",
        "current_job",
        "active_turn",
        "active_event",
        "failed_turn",
        "jobs_error",
        "review_jobs",
        "review_events",
        "review_alias",
        "review_alias_none",
        "review_alias_bool",
        "review_alias_float",
        "active_jobs",
        "counts_missing",
        "bad_count",
        "gap_count",
        "gap_missing",
        "gap_none",
        "gap_bool",
        "gap_float",
        "instruction_invalid",
        "health_failure",
        "bridge_lost_on_health",
    ],
)
def test_paused_reconnect_only_clears_verified_obsolete_bridge_error(monkeypatch, blocker):
    validated = []

    def validate(root, revision):
        validated.append((root, revision))
        if blocker == "instruction_invalid":
            raise wake.WakeError("wake_instruction_bundle_invalid")

    monkeypatch.setattr(wake, "validate_instruction_bundle", validate)

    async def scenario():
        status = {
            "worker_protocol": 2,
            "context_mode": "serial_peer_v1",
            "enabled": blocker == "execution_only",
            "execution_paused": blocker != "intake_only",
            "active_jobs": 0,
            "needs_review_jobs": 0,
            "needs_review_events": 0,
            "gap_count": 0,
        }

        class Jobs:
            connected = False
            last_error = None

            def __init__(self):
                self.changed = asyncio.Event()
                self.health_done = asyncio.Event()
                self.operations = []
                self.cleanup_tasks = set()

            async def request(self, operation, **arguments):
                self.operations.append(operation)
                if operation == "job_status":
                    return {"ok": True, "status": dict(status)}
                assert operation == "job_health", "paused reconnect must not claim or mutate execution"
                self.health_done.set()
                if blocker == "health_failure":
                    raise OSError("synthetic disconnect")
                if blocker == "bridge_lost_on_health":
                    self.connected = False
                return {"ok": True}

        jobs = Jobs()
        app = wake.AppServer(
            wake.WakeConfig(THREAD, instruction_root="/synthetic/instructions", instruction_revision="a" * 40)
        )
        app.connected = True
        app.ready_event.set()
        app.recovery_state = "ready"
        app.request = AsyncMock(side_effect=AssertionError("no AppServer RPC/model calls"))
        dispatcher = wake.DurableWakeDispatcher(app, 123, jobs)
        dispatcher.worker = asyncio.create_task(dispatcher.work())
        try:
            jobs.changed.set()
            await until(lambda: dispatcher.last_error == "wake_bridge_disconnected")
            assert dispatcher.execution_state == "offline" and not validated
            jobs.connected = True
            dispatcher_changes = {
                "other_error": {"last_error": "known_failure"},
                "execution_error": {"execution_error": "known_failure"},
                "active_dispatcher": {"active": True},
                "current_job": {"current_job": {"job_id": "synthetic"}},
            }
            app_changes = {
                "app_disconnected": {"connected": False},
                "blocked": {"blocked": True},
                "recovery_error": {"recovery_error": "known_failure"},
                "recovery_state": {"recovery_state": "recovering"},
                "rpc_error": {"last_rpc_error_code": -32603},
                "outcome_unknown": {"outcome_unknown": True},
                "active_turn": {"active_turn": "synthetic"},
                "active_event": {"active_event_id": "synthetic"},
                "failed_turn": {"failed_turn": wake.FailedTurnContext(event_id="synthetic", turn_id="synthetic")},
            }
            for attribute, value in dispatcher_changes.get(blocker, {}).items():
                setattr(dispatcher, attribute, value)
            for attribute, value in app_changes.get(blocker, {}).items():
                setattr(app, attribute, value)
            if blocker == "not_ready":
                app.ready_event.clear()
            if blocker == "jobs_error":
                jobs.last_error = "known_failure"
            status.update(
                {
                    "review_jobs": {"needs_review_jobs": 1},
                    "review_events": {"needs_review_events": 1},
                    "review_alias": {"needs_review": 1},
                    "review_alias_zero": {"needs_review": 0},
                    "review_alias_none": {"needs_review": None},
                    "review_alias_bool": {"needs_review": False},
                    "review_alias_float": {"needs_review": 0.0},
                    "active_jobs": {"active_jobs": 1},
                    "gap_count": {"gap_count": 1},
                    "gap_none": {"gap_count": None},
                    "gap_bool": {"gap_count": False},
                    "gap_float": {"gap_count": 0.0},
                    "bad_count": {"active_jobs": False},
                }.get(blocker, {})
            )
            if blocker == "counts_missing":
                del status["active_jobs"]
            if blocker == "gap_missing":
                del status["gap_count"]
            jobs.changed.set()
            await asyncio.wait_for(jobs.health_done.wait(), 1)
            public = dispatcher.status()
            healthy = blocker in {None, "intake_only", "execution_only", "review_alias_zero"}
            expected = None if healthy else "wake_bridge_disconnected"
            if blocker == "other_error":
                expected = "known_failure"
            elif blocker == "instruction_invalid":
                expected = "wake_instruction_bundle_invalid"
            assert public["last_error"] == expected
            assert public["ready"] is False and dispatcher.enabled
            assert dispatcher.execution_state == (
                "held" if blocker in {"execution_error", "instruction_invalid"} else "paused"
            )
            assert jobs.operations == (
                ["job_health"]
                if blocker in {"execution_error", "instruction_invalid"}
                else ["job_status", "job_health"]
            )
            app.request.assert_not_called()
            if blocker != "execution_error":
                assert validated == [("/synthetic/instructions", "a" * 40)]
            if healthy:
                assert public["execution_paused"] is status["execution_paused"]
                assert public["intake_enabled"] is status["enabled"]
        finally:
            dispatcher.worker.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher.worker

    asyncio.run(scenario())
