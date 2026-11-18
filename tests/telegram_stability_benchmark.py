"""Opt-in synthetic benchmark using the existing native IPC integration fakes.

Run with the development Python; --source-root can select an immutable Git
archive. No account, AppServer, provider, or production database is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import UTC, datetime
import gc
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time


def distribution(values):
    ordered = sorted(values)
    return {
        "count": len(ordered),
        **{
            name: ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))] if ordered else None
            for name, fraction in (
                ("p50_seconds", 0.5),
                ("p95_seconds", 0.95),
                ("p99_seconds", 0.99),
                ("max_seconds", 1),
            )
        },
    }


def resources():
    return {
        "rss_bytes": int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE"),
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "fds": len(list(Path("/proc/self/fd").iterdir())),
        "threads": threading.active_count(),
        "cpu_seconds": time.process_time(),
    }


def isolate(patch, root):
    """Allow only this run's filesystem SQLite and Unix socket connections."""
    connect, db_connect = socket.socket.connect, sqlite3.connect

    def local_connect(sock, address):
        if not isinstance(address, str) or not Path(address).is_relative_to(root):
            raise AssertionError("benchmark external connection prohibited")
        return connect(sock, address)

    def local_db(database, *args, **kwargs):
        if not Path(database).is_absolute() or not Path(database).is_relative_to(root):
            raise AssertionError("benchmark external database prohibited")
        return db_connect(database, *args, **kwargs)

    def denied(*args, **kwargs):
        raise AssertionError("benchmark external operation prohibited")

    patch.setattr(socket.socket, "connect", local_connect)
    patch.setattr(socket, "getaddrinfo", denied)
    patch.setattr(socket, "create_connection", denied)
    patch.setattr(sqlite3, "connect", local_db)
    patch.setattr(subprocess, "Popen", denied)
    patch.setenv("AUTOSTOP_MANAGER_ENV_FILE", "/dev/null")
    patch.setenv("AUTOSTOP_MANAGER_DB", str(root / "unused.sqlite3"))


def phase_deltas(before, after, expected_counts):
    """Extract an isolated successful sample from two process-local snapshots."""
    assert before["scope"] == after["scope"] == "process"
    assert 0 <= before["observed_seconds"] <= after["observed_seconds"] < math.inf
    assert before["dropped_samples"] == after["dropped_samples"]
    phases = {}
    for name, expected_count in expected_counts.items():
        old = before["new"][name]
        current = after["new"][name]
        assert old["in_flight"] == current["in_flight"] == 0, name
        count = current["count"] - old["count"]
        assert count == expected_count and current["returned"] - old["returned"] == count, name
        assert current["raised"] == old["raised"] and current["cancelled"] == old["cancelled"], name
        assert current.get("unsettled") == old.get("unsettled"), name
        seconds = (current["sum_seconds"] or 0.0) - (old["sum_seconds"] or 0.0)
        assert math.isfinite(seconds) and seconds >= 0, name
        phases[name] = {"seconds": seconds, "count": count}
    return phases


async def _observe_text_reply(runtime, dispatcher, server, peer, incoming, marks):
    from telegram_wake_fakes import until
    from test_telegram_durable_integration import binding, model_complete, read_history, send

    initial = runtime.monitor.record(runtime.client.inbound(incoming, peer=peer))
    assert initial is not None
    marks["accepted"] = time.monotonic()
    completed = dispatcher.completed
    await until(lambda: any(job["state"] == "running" for job in runtime.inbox.execution_slots()))
    (job,) = runtime.inbox.execution_slots()
    assert job["peer_id"] == peer and job["event_ids"] == [initial] and job["state"] == "running"
    await read_history(runtime, job)
    prepared = await send(runtime, job, kind="result", text="synthetic reply")
    receipt = await send(runtime, job, kind="result", text="synthetic reply", dry=prepared)
    assert receipt["verified"] and receipt["reply_to_message_id"] == incoming and not receipt["replayed"]
    marks["receipt_received"] = time.monotonic()
    result = await runtime.jobs.request(
        "job_result", **binding(job), outcome="answered", event_ids=[initial], receipt_keys=[receipt["receipt_key"]]
    )
    assert result["job"]["state"] == "completed"
    await model_complete(server, job)
    await until(lambda: not dispatcher.executions and dispatcher.completed > completed)
    proved = runtime.inbox.read_job(job["job_id"])
    assert proved["execution_proof"] == "completed" and not runtime.inbox.execution_slots()
    assert runtime.inbox.event_state(initial) == "reply_verified"
    assert not dispatcher.unsettled and not dispatcher.cleanup_pending


async def paired_text_latency(root):
    """Use one synthetic peer twice, proving creation then reuse of its binding."""
    import pytest
    from telegram_wake_fakes import MockServer
    from test_telegram_durable_integration import bridge_runtime, bundle, worker

    from autostop_manager import telegram_bridge as bridge

    root.mkdir()
    peer = 101
    cases, marks = [], {}
    server = MockServer(complete=False, peer_threads=True)
    with pytest.MonkeyPatch.context() as patch:
        isolate(patch, root)
        patch.setattr(bridge, "_MUTATION_LOCK", asyncio.Lock())
        instruction_root = bundle.__wrapped__(root, patch)
        async with bridge_runtime(root) as runtime:
            claim, finish = runtime.inbox.claim, runtime.inbox.finish_execution

            def observe_claim(**kwargs):
                job = claim(**kwargs)
                if job is not None:
                    marks["claimed"] = time.monotonic()
                return job

            def observe_proof(**kwargs):
                job = finish(**kwargs)
                marks["proved"] = time.monotonic()
                return job

            patch.setattr(runtime.inbox, "claim", observe_claim)
            patch.setattr(runtime.inbox, "finish_execution", observe_proof)
            started = time.monotonic()
            try:
                async with asyncio.timeout(20), worker(runtime, root, instruction_root, server) as dispatcher:
                    await dispatcher.bootstrap_app.ensure_ready()
                    bootstrap_seconds = time.monotonic() - started
                    retire = dispatcher._retire_completed_child

                    async def observe_retirement(job_id, child):
                        await retire(job_id, child)
                        if job_id not in dispatcher.unsettled:
                            marks["retired"] = time.monotonic()

                    patch.setattr(dispatcher, "_retire_completed_child", observe_retirement)
                    binding = runtime.inbox.read_peer_thread_by_peer(peer)
                    assert binding is None
                    for condition, incoming in (("create_thread", 100), ("reuse_thread", 200)):
                        marks.clear()
                        old_sends = runtime.client.sent
                        bridge_before = (await runtime.jobs.request("job_status"))["status"]["job_latency"]
                        wake_before = dispatcher.status()["stage_timings"]
                        await _observe_text_reply(runtime, dispatcher, server, peer, incoming, marks)
                        bridge_after = (await runtime.jobs.request("job_status"))["status"]["job_latency"]
                        wake_after = dispatcher.status()["stage_timings"]
                        current = runtime.inbox.read_peer_thread_by_peer(peer)
                        assert current is not None and current["state"] == "ready"
                        if binding is not None:
                            assert (binding["thread_id"], binding["revision"]) == (
                                current["thread_id"],
                                current["revision"],
                            )
                        binding = current
                        assert len(server.created_threads) == 1 and runtime.client.sent - old_sends == 1
                        cases.append(
                            {
                                "condition": condition,
                                "verified_replies": 1,
                                "execution_proof": "completed",
                                "driver_seconds": {
                                    "intake_to_claim": marks["claimed"] - marks["accepted"],
                                    "intake_to_verified_reply": marks["receipt_received"] - marks["accepted"],
                                    "terminal_proof_to_cleanup": marks["retired"] - marks["proved"],
                                },
                                "bridge": phase_deltas(
                                    bridge_before,
                                    bridge_after,
                                    dict.fromkeys(
                                        ("history", "history_page", "send", "readback", "reply_to_terminal"), 1
                                    ),
                                ),
                                "wake": phase_deltas(
                                    wake_before,
                                    wake_after,
                                    dict.fromkeys(
                                        (
                                            "peer_prepare",
                                            "idle_wait",
                                            "instruction_delivery",
                                            "turn_start",
                                            "turn_execution",
                                            "finish_request",
                                        ),
                                        1,
                                    ),
                                ),
                            }
                        )
                    assert server.counter == len(cases) == 2 and runtime.inbox.status()["open_events"] == 0
            finally:
                await bridge._effect_executor().close()
    return {"bootstrap_ready_seconds": bootstrap_seconds, "cases": cases}


async def pipeline(root, clients, messages, actor_lanes):  # noqa: C901 - one measured IPC lifecycle and cleanup.
    import pytest
    from telegram_wake_fakes import MockServer
    from test_telegram_durable_integration import (
        binding,
        bridge_runtime,
        bundle,
        model_complete,
        read_history,
        send,
        worker,
    )

    from autostop_manager import telegram_bridge as bridge

    root.mkdir()
    samples, accepted, claimed, seen, tasks = defaultdict(list), {}, {}, set(), set()
    baseline_tasks = set(asyncio.all_tasks())
    before = resources()
    started = time.monotonic()
    peak_pending, peak_tasks, maximum_lag, covered = 0, 0, 0.0, 0
    stop = asyncio.Event()

    async def heartbeat():
        nonlocal maximum_lag
        while not stop.is_set():
            tick = time.monotonic()
            await asyncio.sleep(0.01)
            maximum_lag = max(maximum_lag, time.monotonic() - tick - 0.01)

    async def control_probe(jobs):
        for _ in range(10):
            if stop.is_set():
                return
            tick = time.monotonic()
            result = await jobs.request("job_status")
            assert result["ok"]
            samples["control_rpc"].append(time.monotonic() - tick)
            await asyncio.sleep(0.02)

    with pytest.MonkeyPatch.context() as patch:
        isolate(patch, root)
        patch.setattr(bridge, "_MUTATION_LOCK", asyncio.Lock())
        instruction_root = bundle.__wrapped__(root, patch)
        bundle_ready = time.monotonic()
        async with bridge_runtime(root) as runtime:
            bridge_ready = time.monotonic()
            claim, proof = runtime.inbox.claim, runtime.inbox.finish_execution

            def observe_claim(**kwargs):
                job = claim(**kwargs)
                if job:
                    claimed[job["job_id"]] = time.monotonic()
                    samples["queue_wait"].append(claimed[job["job_id"]] - min(accepted[e] for e in job["event_ids"]))
                return job

            def observe_proof(**kwargs):
                value = proof(**kwargs)
                samples["accepted_to_terminal_proof"].append(
                    time.monotonic() - min(accepted[e] for e in value["event_ids"])
                )
                return value

            patch.setattr(runtime.inbox, "claim", observe_claim)
            patch.setattr(runtime.inbox, "finish_execution", observe_proof)
            for peer in range(101, 101 + clients):
                for number in range(messages):
                    tick = time.monotonic()
                    event = runtime.monitor.record(
                        runtime.client.inbound(peer * messages + number, peer=peer, text="synthetic request")
                    )
                    accepted[event] = tick
                    samples["intake"].append(time.monotonic() - tick)
            peak_pending = runtime.inbox.status()["open_events"]
            intake_finished = time.monotonic()
            server = MockServer(complete=False, peer_threads=True)
            lanes, fast_done = asyncio.Semaphore(actor_lanes), asyncio.Event()
            heart = asyncio.create_task(heartbeat())
            control = asyncio.create_task(control_probe(runtime.jobs))

            async def answer(job):
                nonlocal covered
                # One slow fake actor waits for another client's verified result.
                if clients > 1 and job["peer_id"] == 101:
                    await fast_done.wait()
                async with lanes:
                    await read_history(runtime, job)
                    samples["claim_to_context_ready"].append(time.monotonic() - claimed[job["job_id"]])
                    for kind in ("ack", "result"):
                        tick = time.monotonic()
                        dry = await send(runtime, job, kind=kind, text="synthetic " + kind)
                        receipt = await send(runtime, job, kind=kind, text="synthetic " + kind, dry=dry)
                        assert receipt["verified"]
                        samples[kind + "_prepare_and_readback"].append(time.monotonic() - tick)
                        samples["accepted_to_" + kind + "_receipt"].append(
                            time.monotonic() - min(accepted[e] for e in job["event_ids"])
                        )
                    await runtime.jobs.request(
                        "job_result",
                        **binding(job),
                        outcome="answered",
                        event_ids=job["event_ids"],
                        receipt_keys=[receipt["receipt_key"]],
                    )
                    covered += len(job["event_ids"])
                    fast_done.set()
                    await model_complete(server, job)

            try:
                async with asyncio.timeout(180), worker(runtime, root, instruction_root, server) as dispatcher:
                    while (
                        covered < clients * messages
                        or dispatcher.completed < len(seen)
                        or runtime.inbox.execution_slots()
                    ):
                        for job in runtime.inbox.execution_slots():
                            if job["state"] == "running" and job["job_id"] not in seen:
                                seen.add(job["job_id"])
                                tasks.add(asyncio.create_task(answer(job)))
                        for task in tasks:
                            if task.done():
                                task.result()
                        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
                        await asyncio.sleep(0.002)
                    await asyncio.gather(*tasks)
                    status = runtime.inbox.status()
                    assert status["open_events"] == 0 and not runtime.inbox.execution_slots(), (
                        status,
                        runtime.inbox.execution_slots(),
                        dispatcher.status(),
                    )
                    assert status["reply_verified_events"] == clients * messages
                    assert runtime.client.sent == 2 * len(seen) and server.counter == len(seen)
                    timing_snapshot = dispatcher.status()["stage_timings"]
                    processing_finished = time.monotonic()
                worker_closed = time.monotonic()
            finally:
                stop.set()
                await heart
                await control
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await bridge._effect_executor().close()
                effects_closed = time.monotonic()
        bridge_closed = time.monotonic()
    isolation_closed = time.monotonic()
    # Closing connections can leave already scheduled finalizers for one turn.
    for _ in range(10):
        if not (set(asyncio.all_tasks()) - baseline_tasks):
            break
        await asyncio.sleep(0.01)
    remaining = set(asyncio.all_tasks()) - baseline_tasks
    assert not remaining, [(task.get_coro().__qualname__, task.done()) for task in remaining]
    tasks_settled = time.monotonic()
    gc.collect()
    gc_finished = time.monotonic()
    after = resources()
    assert after["fds"] <= before["fds"] and after["threads"] == before["threads"]
    elapsed = time.monotonic() - started
    return {
        "clients": clients,
        "messages_per_client": messages,
        "actor_lanes": actor_lanes,
        "elapsed_seconds": elapsed,
        "phase_seconds": {
            "bundle_setup": bundle_ready - started,
            "bridge_setup": bridge_ready - bundle_ready,
            "intake": intake_finished - bridge_ready,
            "processing": processing_finished - intake_finished,
            "worker_shutdown": worker_closed - processing_finished,
            "effects_shutdown": effects_closed - worker_closed,
            "bridge_shutdown": bridge_closed - effects_closed,
            "isolation_exit": isolation_closed - bridge_closed,
            "task_settle": tasks_settled - isolation_closed,
            "gc": gc_finished - tasks_settled,
        },
        "events_per_second": clients * messages / elapsed,
        "stages": {name: distribution(values) for name, values in samples.items()},
        "stage_timings": timing_snapshot,
        "before": before,
        "after": after,
        "peak_pending": peak_pending,
        "peak_tasks": peak_tasks,
        "event_loop_lag_max_seconds": maximum_lag,
        "leaked_tasks": 0,
        "verified_sends": len(seen) * 2,
        "covered_events": clients * messages,
    }


def database(root, retained):
    import pytest

    from autostop_manager.telegram_inbox import TelegramInbox

    root.mkdir()
    now = datetime(2026, 11, 17, tzinfo=UTC)
    with pytest.MonkeyPatch.context() as patch:
        isolate(patch, root)
        with TelegramInbox(root / "inbox.sqlite3", clock=lambda: now) as inbox:
            inbox.duty_set(True)
            with inbox._db:
                inbox._db.executemany(
                    "INSERT INTO peers VALUES (?,1)", ((peer,) for peer in range(100, 100 + retained))
                )
                inbox._db.executemany(
                    "INSERT INTO jobs(job_id,peer_id,state,revision,lease,context_revision,instruction_root,"
                    "instruction_revision,created_at,updated_at,context_mode) "
                    "VALUES (?,?,'completed',1,'synthetic',1,'/synthetic',?,?,?,'peer_v1')",
                    (
                        (f"history-{peer}", peer, "a" * 40, now.isoformat(), now.isoformat())
                        for peer in range(100, 100 + retained)
                    ),
                )
                inbox._db.executemany(
                    "INSERT INTO events(sequence,peer_id,message_id,received_at,state,context_revision,job_id,completed_at) "
                    "VALUES (?,?,1,?,'reply_verified',1,?,?)",
                    (
                        (str(peer), peer, now.isoformat(), f"history-{peer}", now.isoformat())
                        for peer in range(100, 100 + retained)
                    ),
                )
            measurements = {}
            for phase in ("completed_history", "quiet_peers"):
                values = []
                for _ in range(5):
                    tick = time.monotonic()
                    status = inbox.status()
                    values.append(time.monotonic() - tick)
                assert status["held_peers"] == 0
                measurements[phase + "_status"] = distribution(values)
                if phase == "completed_history":
                    with inbox._db:
                        inbox._db.execute("DELETE FROM events")
                        inbox._db.execute("DELETE FROM jobs")
            tick = time.monotonic()
            event = inbox.record(2, 1)
            measurements["intake_seconds"] = time.monotonic() - tick
            tick = time.monotonic()
            job = inbox.claim(instruction_root="/synthetic", instruction_revision="a" * 40)
            measurements["claim_seconds"] = time.monotonic() - tick
            assert job["event_ids"] == [event]
            assert not list(inbox._db.execute("PRAGMA foreign_key_check"))
            measurements["database_bytes"] = sum(path.stat().st_size for path in root.iterdir() if path.is_file())
    return {"retained_peers": retained, **measurements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 4))
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 10, 50, 100])
    parser.add_argument("--history", type=int, nargs="+", default=[1000, 10000, 100000])
    parser.add_argument(
        "--paired-text", action="store_true", help="run the isolated create/reuse reply pair per repeat"
    )
    args = parser.parse_args()
    source = args.source_root.resolve()
    timings_source = source / "autostop_manager/telegram_timings.py"
    if args.paired_text and not timings_source.is_file():
        parser.error("--paired-text requires job latency timings in the selected source")
    sys.path[:0] = [str(source / "tests"), str(source)]
    os.environ["AUTOSTOP_MANAGER_ENV_FILE"] = "/dev/null"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    assert all(0 < n <= 100 for n in args.clients) and all(0 < n <= 100000 for n in args.history)
    report = {
        "format": (
            "autostop.manager.synthetic-text-latency.v1"
            if args.paired_text
            else "autostop.manager.synthetic-stability-benchmark.v1"
        ),
        "source_root": str(source),
        "python": sys.version,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "synthetic_only": True,
        "model_or_live_delivery_proof": False,
        "repeats": [],
        "dependencies": {name: version(name) for name in ("pytest", "websockets", "telethon", "mcp")},
        "source_files_sha256": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in (
                "autostop_manager/telegram_bridge.py",
                "autostop_manager/telegram_inbox.py",
                "autostop_manager/telegram_wake.py",
                "autostop_manager/telegram_effects.py",
                "tests/telegram_wake_fakes.py",
                "tests/test_telegram_durable_integration.py",
            )
        },
    }
    if timings_source.is_file():
        report["source_files_sha256"]["autostop_manager/telegram_timings.py"] = hashlib.sha256(
            timings_source.read_bytes()
        ).hexdigest()
    from autostop_manager import telegram_bridge

    assert Path(telegram_bridge.__file__).resolve().is_relative_to(source)
    with tempfile.TemporaryDirectory(prefix="mng-bench-") as temporary:
        root = Path(temporary)
        for repetition in range(args.repeats):
            if args.paired_text:
                pair = asyncio.run(paired_text_latency(root / f"pair{repetition}"))
                report["repeats"].append({"paired_text": pair})
                continue
            group = {"pipeline": [], "database": []}
            for clients in args.clients:
                value = asyncio.run(pipeline(root / f"p{repetition}-{clients}", clients, 10, 8))
                group["pipeline"].append(value)
                print(
                    json.dumps(
                        {"repeat": repetition, "clients": clients, "seconds": round(value["elapsed_seconds"], 3)}
                    ),
                    flush=True,
                )
            for history in args.history:
                group["database"].append(database(root / f"d{repetition}-{history}", history))
            report["repeats"].append(group)
    report["temporary_removed"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
