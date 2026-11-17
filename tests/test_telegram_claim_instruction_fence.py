"""A stale wake cannot claim fresh input, while old bound work stays readable."""

import asyncio

import pytest

from autostop_manager import telegram_bridge as bridge
from autostop_manager.telegram_inbox import TelegramInbox


OLD_ROOT, OLD_REVISION = "/test/previous", "a" * 40
ROOT, REVISION = "/test/current", "b" * 40
ERROR = "wake_instruction_release_mismatch"


def claim(root=ROOT, revision=REVISION):
    return {
        "operation": "job_claim",
        "instruction_root": root,
        "instruction_revision": revision,
        "worker_protocol": 3,
    }


def guard(job):
    return {"job_id": job["job_id"], "lease": job["lease"], "expected_revision": job["revision"]}


async def invoke(inbox, request):
    monitor = bridge.DurableInboundMonitor(inbox, enabled=True)
    config = bridge.TelegramConfig(api_id=123456, api_hash="0" * 32)
    return await bridge._handle_operation(object(), config, request, inbound_monitor=monitor)


async def running(inbox, root=OLD_ROOT, revision=OLD_REVISION):
    job = (await invoke(inbox, claim(root, revision)))["job"]
    mapping = inbox.prepare_peer_thread(**guard(job))
    inbox.bind_peer_thread(**guard(job), creation_key=mapping["creation_key"], thread_id="old-thread")
    job = inbox.start_intent(**guard(job), thread_id="old-thread")
    return inbox.bind_turn(**guard(job), turn_id="old-turn")


@pytest.mark.parametrize(
    ("root", "revision"),
    [(OLD_ROOT, OLD_REVISION), (ROOT, OLD_REVISION), (OLD_ROOT, REVISION)],
    ids=["old-wake", "wrong-revision", "wrong-physical-root"],
)
def test_stale_new_claim_holds_health_without_binding_or_consuming_input(tmp_path, root, revision):
    async def scenario():
        with TelegramInbox(tmp_path / "state" / "inbox.sqlite3") as inbox:
            inbox.configure_instructions(ROOT, REVISION)
            inbox.duty_set(True)
            event = inbox.record(42, 1)
            reference = inbox.resolve(event)
            for _ in range(2):
                with pytest.raises(bridge.BridgeError, match=f"^{ERROR}$"):
                    await invoke(inbox, claim(root, revision))
                assert inbox.active_jobs() == [] and inbox.execution_slots() == []
                assert inbox.event_state(event) == "open" and inbox.resolve(event) == reference
                assert inbox.health()["codex"] == {"state": "held", "error_code": ERROR}
                assert not inbox.status()["execution_paused"]
            assert len(inbox.pending_notifications()) == 1  # one technical incident, no retry storm

    asyncio.run(scenario())


def test_bridge_without_its_own_instruction_pin_cannot_claim_input(tmp_path):
    async def scenario():
        with TelegramInbox(tmp_path / "state" / "inbox.sqlite3") as inbox:
            inbox.duty_set(True)
            event = inbox.record(42, 1)
            with pytest.raises(bridge.BridgeError, match=f"^{ERROR}$"):
                await invoke(inbox, claim())
            assert inbox.active_jobs() == [] and inbox.event_state(event) == "open"

    asyncio.run(scenario())


def test_current_wake_claims_fresh_input_with_the_bridge_pin(tmp_path):
    async def scenario():
        with TelegramInbox(tmp_path / "state" / "inbox.sqlite3") as inbox:
            inbox.configure_instructions(ROOT, REVISION)
            inbox.duty_set(True)
            event = inbox.record(42, 1)
            job = (await invoke(inbox, claim()))["job"]
            assert job["instruction_root"] == ROOT and job["instruction_revision"] == REVISION
            assert job["event_ids"] == [event] and job["state"] == "claimed"

    asyncio.run(scenario())


@pytest.mark.parametrize("has_input", [False, True])
def test_correct_caller_repairs_only_instruction_hold_even_when_queue_is_empty(tmp_path, has_input):
    async def scenario():
        with TelegramInbox(tmp_path / "state" / "inbox.sqlite3") as inbox:
            inbox.configure_instructions(ROOT, REVISION)
            inbox.duty_set(True)
            if has_input:
                inbox.record(42, 1)
            with pytest.raises(bridge.BridgeError, match=f"^{ERROR}$"):
                await invoke(inbox, claim(OLD_ROOT, OLD_REVISION))
            assert inbox.health()["codex"] == {"state": "held", "error_code": ERROR}

            result = await invoke(inbox, claim())

            assert (result["job"] is not None) is has_input
            assert inbox.health()["codex"] == {"state": "connected", "error_code": None}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "error"),
    [("held", "wake_cleanup_pending"), ("offline", "codex_disconnected"), ("paused", None)],
)
def test_instruction_mismatch_and_correct_caller_do_not_clear_another_hold(tmp_path, state, error):
    async def scenario():
        with TelegramInbox(tmp_path / "state" / "inbox.sqlite3") as inbox:
            inbox.configure_instructions(ROOT, REVISION)
            inbox.duty_set(True)
            inbox.record(42, 1)
            inbox.set_health("codex", state, error)
            before = inbox.health()

            with pytest.raises(bridge.BridgeError, match=f"^{ERROR}$"):
                await invoke(inbox, claim(OLD_ROOT, OLD_REVISION))
            assert inbox.health() == before
            assert (await invoke(inbox, claim()))["job"] is not None
            assert inbox.health() == before

    asyncio.run(scenario())


def test_correct_caller_does_not_resolve_another_peers_unknown_send(tmp_path):
    async def scenario():
        with TelegramInbox(tmp_path / "state" / "inbox.sqlite3") as inbox:
            inbox.configure_instructions(OLD_ROOT, OLD_REVISION)
            inbox.duty_set(True)
            event = inbox.record(42, 1)
            job = await running(inbox)
            key = inbox.reserve_effect(
                **guard(job), kind="result", text_sha256="c" * 64, peer_id=42, reply_to_message_id=1
            )
            inbox.mark_effect_intent(key)
            job = inbox.needs_review(**guard(job), reason_code="inbox_effect_outcome_unknown")
            inbox.set_health("codex", "held", "inbox_effect_outcome_unknown")
            before_health, before_job = inbox.health(), inbox.read_job(job["job_id"])
            inbox.configure_instructions(ROOT, REVISION)
            inbox.record(43, 1)

            with pytest.raises(bridge.BridgeError, match=f"^{ERROR}$"):
                await invoke(inbox, claim(OLD_ROOT, OLD_REVISION))
            assert (await invoke(inbox, claim()))["job"]["peer_id"] == 43

            assert inbox.health() == before_health
            assert inbox.read_job(job["job_id"]) == before_job
            assert inbox.read_effect(key)["attempted"] and not inbox.read_effect(key)["verified"]
            assert inbox.execution_slot(peer_id=42) is not None
            assert inbox.event_state(event) == "open"

    asyncio.run(scenario())


def test_new_bridge_preserves_old_active_pin_receipt_and_stop_proof_after_restart(tmp_path):
    async def scenario():
        path = tmp_path / "state" / "inbox.sqlite3"
        with TelegramInbox(path) as inbox:
            inbox.configure_instructions(OLD_ROOT, OLD_REVISION)
            inbox.duty_set(True)
            event = inbox.record(42, 1)
            job = await running(inbox)
            key = inbox.reserve_effect(
                **guard(job), kind="ack", text_sha256="c" * 64, peer_id=42, reply_to_message_id=1
            )
            inbox.mark_effect_intent(key)
            inbox.verify_effect(effect_key=key, receipt_key=key, message_id=2)
            job = inbox.needs_review(**guard(job), reason_code="synthetic_restart")
            effects = job["effects"]

        with TelegramInbox(path) as inbox:
            inbox.configure_instructions(ROOT, REVISION)
            new_event = inbox.record(43, 1)
            with pytest.raises(bridge.BridgeError, match=f"^{ERROR}$"):
                await invoke(inbox, claim(OLD_ROOT, OLD_REVISION))
            current = (await invoke(inbox, {"operation": "job_read", "job_id": job["job_id"]}))["job"]
            assert current["instruction_root"] == OLD_ROOT and current["instruction_revision"] == OLD_REVISION
            assert current["effects"] == effects
            proven = (
                await invoke(
                    inbox,
                    {
                        "operation": "job_execution_proof",
                        **guard(current),
                        "proof_kind": "terminal",
                        "thread_id": "old-thread",
                        "turn_id": "old-turn",
                        "terminal_status": "completed",
                        "local_tasks_done": True,
                    },
                )
            )["job"]
            assert proven["execution_proof"] == "completed" and inbox.execution_slots() == []
            assert proven["instruction_root"] == OLD_ROOT and proven["instruction_revision"] == OLD_REVISION
            assert proven["effects"] == effects and proven["state"] == "needs_review"
            assert inbox.event_state(event) == inbox.event_state(new_event) == "open"
            assert inbox.health()["codex"] == {"state": "held", "error_code": ERROR}

    asyncio.run(scenario())
