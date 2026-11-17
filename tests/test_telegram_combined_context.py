"""Combined transport races with local IPC and synthetic Telegram messages only."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from autostop_manager import telegram_bridge as bridge, telegram_wake as wake
from test_telegram_durable_integration import (
    CLIENT,
    OTHER,
    OWNER,
    binding,
    bridge_runtime,
    claim_running,
    send,
)
from test_telegram_durable_integration import bundle as bundle
from telegram_wake_fakes import until


@pytest.fixture(autouse=True)
def local_transport_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    connect = socket.socket.connect

    def local_connect(sock, address):
        assert sock.family == socket.AF_UNIX, "external transport is forbidden"
        assert str(address).startswith(str(tmp_path) + "/"), "socket must belong to this synthetic case"
        return connect(sock, address)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real account configuration and subprocesses are forbidden")

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(bridge, "_config_for_account", forbidden)


@pytest.mark.parametrize("first_unknown", [False, True], ids=["verified-control", "unknown-recipient"])
def test_handoff_rechecks_shared_recipient_after_client_context_await(tmp_path, bundle, first_unknown):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            jobs = []
            for message_id, peer in ((100, CLIENT), (200, OTHER)):
                runtime.monitor.record(runtime.client.inbound(message_id, peer=peer))
                jobs.append(await claim_running(runtime, bundle))

            requests = [
                binding(job)
                | {
                    "reply_to_message_id": message_id,
                    "text": f"Synthetic handoff {peer}",
                    "mode": "dry_run",
                }
                for job, message_id, peer in zip(jobs, (100, 200), (CLIENT, OTHER), strict=True)
            ]
            dry = [await runtime.jobs.request("job_handoff", **request) for request in requests]
            applied = [
                request
                | {
                    "mode": "apply",
                    "contract_token": result["contract_token"],
                    "idempotency_key": result["idempotency_key"],
                }
                for request, result in zip(requests, dry, strict=True)
            ]
            entered, release = asyncio.Event(), asyncio.Event()
            original = runtime.client.get_messages
            other_exact_reads = 0

            async def blocked(entity, *, ids=None, limit=None, offset_id=None):
                nonlocal other_exact_reads
                result = await original(entity, ids=ids, limit=limit, offset_id=offset_id)
                if getattr(entity, "id", entity) == OTHER and isinstance(ids, list):
                    other_exact_reads += 1
                    # First exact read binds the job; the second is final
                    # revalidation after the owner's send guard has passed.
                    if other_exact_reads == 2:
                        entered.set()
                        await release.wait()
                return result

            runtime.client.get_messages = blocked
            second = asyncio.create_task(runtime.jobs.request("job_handoff", **applied[1]))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                runtime.client.lose_send_response = first_unknown
                if first_unknown:
                    with pytest.raises(wake.WakeError, match=r"^telegram_operation_failed$"):
                        await runtime.jobs.request("job_handoff", **applied[0])
                else:
                    assert (await runtime.jobs.request("job_handoff", **applied[0]))["verified"]
                runtime.client.lose_send_response = False
                assert runtime.client.sent == 1
                release.set()
                if first_unknown:
                    with pytest.raises(wake.WakeError, match=r"^send_outcome_uncertain$"):
                        await second
                    assert runtime.client.sent == 1
                    assert not runtime.inbox.read_effect(dry[1]["idempotency_key"])["attempted"]
                else:
                    assert (await second)["verified"] and runtime.client.sent == 2
                assert all(message.chat_id == OWNER for message in runtime.client.messages.values() if message.out)
                assert all(runtime.inbox.read_job(job["job_id"])["state"] == "running" for job in jobs)
            finally:
                release.set()
                await asyncio.gather(second, return_exceptions=True)
                await bridge._effect_executor().close()

    asyncio.run(scenario())


async def _change_context(runtime, job, change):
    identifier = max(runtime.client.messages) + 1
    if change == "incoming":
        runtime.monitor.record(runtime.client.inbound(identifier, peer=CLIENT, text="Synthetic added request"))
    elif change == "edit":
        source = runtime.client.messages[100]
        source.message = "Synthetic edited request"
        runtime.monitor.remember_context(source)
    elif change == "delete":
        runtime.client.messages.pop(100)
        runtime.monitor.deleted([100], CLIENT)
    elif change in {"guidance", "takeover"}:
        await runtime.jobs.request(
            "job_client_control",
            job_id=job["job_id"],
            action="context" if change == "guidance" else "takeover",
            text="Synthetic trusted guidance" if change == "guidance" else None,
            expected_control_revision=0,
        )
    elif change == "manual":
        runtime.monitor.self_peer_id = 900
        message = runtime.client.inbound(identifier, peer=CLIENT, text="Synthetic partial human answer")
        message.out, message.sender_id = True, 900
        runtime.monitor.observe_self_message(message)
    else:
        assert change == "unchanged"


async def _finish_other_peer(runtime, job):
    dry = await send(runtime, job, kind="result", text="Synthetic independent answer")
    proof = await send(runtime, job, kind="result", text="Synthetic independent answer", dry=dry)
    assert runtime.client.messages[proof["message_id"]].chat_id == OTHER
    result = await runtime.jobs.request(
        "job_result",
        **binding(job),
        outcome="answered",
        event_ids=job["event_ids"],
        receipt_keys=[proof["receipt_key"]],
    )
    assert result["job"]["state"] == "completed"


@pytest.mark.parametrize("stage", ["history", "download"])
@pytest.mark.parametrize("change", ["unchanged", "incoming", "edit", "delete", "guidance", "manual"])
def test_context_change_during_history_or_cancelled_download_keeps_other_peer_independent(
    tmp_path, bundle, stage, change
):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            source = runtime.client.inbound(100)
            content = b"%PDF-1.7\nsynthetic attachment"
            source.media = SimpleNamespace()
            source.file = SimpleNamespace(name="source.pdf", mime_type="application/pdf", size=len(content))
            source.document = SimpleNamespace(id=123, dc_id=1, attributes=[])
            first = runtime.monitor.record(source)
            job = await claim_running(runtime, bundle)
            runtime.monitor.record(runtime.client.inbound(200, peer=OTHER))
            other = await claim_running(runtime, bundle)
            entered, release = asyncio.Event(), asyncio.Event()
            original = runtime.client.get_messages

            async def history(entity, *, ids=None, limit=None, offset_id=None):
                rows = await original(entity, ids=ids, limit=limit, offset_id=offset_id)
                if getattr(entity, "id", entity) == CLIENT and ids is None and limit == 101:
                    entered.set()
                    await release.wait()
                return rows

            async def download(message, *, file):
                assert message.id == 100 and message.chat_id == CLIENT and file is bytes
                entered.set()
                await release.wait()
                return content

            request = {"peer": str(CLIENT), "job_id": job["job_id"], "lease": job["lease"], "revision": job["revision"]}
            if stage == "history":
                runtime.client.get_messages = history
                operation = "read"
                request["limit"] = 100
            else:
                runtime.client.download_media = download
                operation = "download"
                request.update(message_id=100, mode="dry_run")
                dry = await runtime.jobs.request(operation, **request)
                request.update(mode="apply", idempotency_key="synthetic-download", contract_token=dry["contract_token"])
            caller = asyncio.create_task(runtime.jobs.request(operation, **request))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await _change_context(runtime, job, change)
                if stage == "download":
                    caller.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await caller
                    assert bridge._effect_executor().has_job_work(job["job_id"])
                await _finish_other_peer(runtime, other)
                assert runtime.inbox.event_state(first) == "open"
                release.set()
                if stage == "history":
                    if change == "unchanged":
                        page = await caller
                        await runtime.jobs.request(
                            "job_history_confirm", **binding(job), history_receipt=page["history_receipt"]
                        )
                        assert runtime.monitor.context_jobs[job["job_id"]]["complete"]
                    else:
                        with pytest.raises(wake.WakeError, match=r"^inbox_context_stale$"):
                            await caller
                        assert not runtime.monitor.context_jobs[job["job_id"]]["complete"]
                else:
                    await until(lambda: not bridge._effect_executor().has_job_work(job["job_id"]))
                    ledger = bridge._load_idempotency(runtime.config.state_dir / "idempotency.json")
                    if change == "unchanged":
                        saved = Path(ledger["synthetic-download"]["saved_path"])
                        assert saved.read_bytes() == content
                        assert (await runtime.jobs.request("discard_download", path=str(saved)))["removed"]
                        assert not saved.exists()
                    else:
                        assert "synthetic-download" not in ledger
                    assert not list((runtime.config.socket_path.parent / "inbox").glob("*"))
                assert runtime.client.sent == 1
                assert runtime.inbox.read_job(job["job_id"])["state"] == "running"
                assert not any(effect["attempted"] for effect in runtime.inbox.read_job(job["job_id"])["effects"])
            finally:
                release.set()
                await asyncio.gather(caller, return_exceptions=True)
                await bridge._effect_executor().close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["unchanged", "incoming", "edit", "delete", "guidance", "manual", "takeover"])
def test_context_change_and_caller_cancel_during_readback_preserve_only_exact_receipt(tmp_path, bundle, change):
    async def scenario():
        async with bridge_runtime(tmp_path) as runtime:
            first = runtime.monitor.record(runtime.client.inbound(100))
            job = await claim_running(runtime, bundle)
            runtime.monitor.record(runtime.client.inbound(200, peer=OTHER))
            other = await claim_running(runtime, bundle)
            dry = await send(runtime, job, kind="result", text="Synthetic original answer")
            entered, release = asyncio.Event(), asyncio.Event()
            original = runtime.client.get_messages

            async def readback(entity, *, ids=None, limit=None, offset_id=None):
                result = await original(entity, ids=ids, limit=limit, offset_id=offset_id)
                if (
                    getattr(entity, "id", entity) == CLIENT
                    and isinstance(ids, int)
                    and result is not None
                    and result.out
                ):
                    entered.set()
                    await release.wait()
                return result

            runtime.client.get_messages = readback
            caller = asyncio.create_task(send(runtime, job, kind="result", text="Synthetic original answer", dry=dry))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await _change_context(runtime, job, change)
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                with pytest.raises(wake.WakeError, match=r"^inbox_execution_effect_active$"):
                    await runtime.jobs.request("job_execution_proof", **binding(job), local_tasks_done=True)
                await _finish_other_peer(runtime, other)
                assert runtime.inbox.event_state(first) == "open" and runtime.client.sent == 2
                release.set()
                await until(lambda: not bridge._effect_executor().has_job_work(job["job_id"]))
                effect = runtime.inbox.read_effect(dry["idempotency_key"])
                assert effect["verified"] and effect["attempted"] and effect["receipt_key"] == dry["idempotency_key"]
                proof = await runtime.jobs.request(
                    "job_effect_readback", **binding(job), effect_key=dry["idempotency_key"]
                )
                assert proof["verified"] and proof["message_id"] == effect["sent_message_id"]
                request = binding(job) | {
                    "outcome": "answered",
                    "event_ids": [first],
                    "receipt_keys": [proof["receipt_key"]],
                }
                if change == "unchanged":
                    assert (await runtime.jobs.request("job_result", **request))["job"]["state"] == "completed"
                    assert runtime.inbox.event_state(first) == "reply_verified"
                else:
                    error = "inbox_guidance_unread" if change == "guidance" else "inbox_context_stale"
                    with pytest.raises(wake.WakeError, match=f"^{error}$"):
                        await runtime.jobs.request("job_result", **request)
                    assert runtime.inbox.event_state(first) == "open"
                    assert runtime.inbox.read_job(job["job_id"])["state"] == "running"
                assert runtime.client.sent == 2
            finally:
                release.set()
                with suppress(asyncio.CancelledError):
                    await caller
                await bridge._effect_executor().close()

    asyncio.run(scenario())
