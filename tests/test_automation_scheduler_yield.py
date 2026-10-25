from __future__ import annotations

import asyncio

import pytest

from autostop_manager.automation_daemon import AutomationDaemon


@pytest.mark.parametrize("mode", ["reconcile", "outbox", "run"])
@pytest.mark.parametrize("stop_from_control", [False, True])
def test_completed_work_yields_to_control_before_claiming_another_item(mode, stop_from_control):
    batch_size = 5
    started = []
    completed = []
    observed = []

    def control():
        observed.append((list(started), list(completed)))
        if stop_from_control:
            daemon.stopping.set()

    def claim():
        item = len(started) + 1
        started.append(item)
        if item == 1:
            asyncio.get_running_loop().call_soon(control)
        return {"item": item}

    def finish(work):
        completed.append(work["item"])
        if len(completed) == batch_size:
            daemon.stopping.set()

    class Store:
        def controller_heartbeat(self, **_values):
            pass

        def reconcile_next_job(self):
            if mode != "reconcile":
                return None
            work = claim()
            finish(work)
            return {"applied": True}

        def claim_outbox(self, **_values):
            return claim() if mode == "outbox" else None

        def claim_next_run(self, **_values):
            return claim() if mode == "run" else None

    daemon = AutomationDaemon(store=Store(), timer_controller=object(), control_server=object())

    async def execute(work):
        # An awaited work coroutine is allowed to complete without suspending.
        finish(work)

    daemon._execute_claim = execute
    daemon._execute_outbox = execute

    async def scenario():
        await asyncio.wait_for(daemon.scheduler_loop(), timeout=5)
        await asyncio.sleep(0)

    asyncio.run(scenario())

    # Offer control only after the current work is complete, before the next
    # claim. A stop callback must leave the remaining batch untouched.
    assert observed == [([1], [1])]
    expected = [1] if stop_from_control else list(range(1, batch_size + 1))
    assert started == completed == expected


def test_cancellation_after_one_finished_item_removes_the_heartbeat_task():
    finished = []
    claims = []

    class Store:
        def controller_heartbeat(self, **_values):
            pass

        def reconcile_next_job(self):
            return None

        def claim_outbox(self, **_values):
            return None

        def claim_next_run(self, **_values):
            claims.append(len(claims) + 1)
            return {
                "job_id": "synthetic-job",
                "run_id": f"synthetic-run-{claims[-1]}",
                "template_id": "synthetic-unconfigured",
                "fencing_token": 1,
            }

        def finish_run(self, **values):
            finished.append(values)
            if len(finished) == 1:
                asyncio.get_running_loop().call_soon(scheduler.cancel)
            if len(finished) == 5:
                daemon.stopping.set()

    daemon = AutomationDaemon(store=Store(), timer_controller=object(), control_server=object())
    scheduler = None

    async def scenario():
        nonlocal scheduler
        existing_tasks = asyncio.all_tasks()
        scheduler = asyncio.create_task(daemon.scheduler_loop())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(scheduler, timeout=5)
        await asyncio.sleep(0)
        assert not asyncio.all_tasks().difference(existing_tasks)

    asyncio.run(scenario())
    assert claims == [1]
    assert len(finished) == 1
    assert finished[0]["result_code"] == "automation_executor_unavailable"
    assert finished[0]["succeeded"] is False
    assert finished[0]["fencing_token"] == 1
