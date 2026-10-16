"""Temporary observation-only CI triage; never a product change."""

from __future__ import annotations

import functools
import gc
import json
import time
from typing import Any

import pytest

from autostop_manager import j1_fetch


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: Any):
    if item.nodeid != ("tests/test_j1_http_deadline.py::test_http_exchange_deadline_bounds_continuous_drip[headers]"):
        yield
        return
    events: list[dict[str, Any]] = []
    patch = pytest.MonkeyPatch()

    def stamp() -> dict[str, float]:
        return {
            "wall": time.monotonic(),
            "thread_cpu": time.thread_time(),
            "process_cpu": time.process_time(),
        }

    def observe_gc(phase: str, info: dict[str, Any]) -> None:
        events.append({"name": "gc_" + phase, **stamp(), "info": dict(info)})

    def wrap(owner: Any, name: str, label: str) -> None:
        original = getattr(owner, name)

        @functools.wraps(original)
        def observed(*args: Any, **kwargs: Any) -> Any:
            row: dict[str, Any] = {
                "name": label,
                "start": stamp(),
                "error": None,
            }
            if args and hasattr(args[0], "_j1_deadline"):
                row["deadline"] = args[0]._j1_deadline
            if args and hasattr(args[0], "_deadline"):
                row["deadline"] = args[0]._deadline
            try:
                value = original(*args, **kwargs)
                if label == "raw_readinto":
                    row["bytes"] = value
                return value
            except BaseException as exc:
                row["error"] = type(exc).__name__ + ":" + str(exc)
                raise
            finally:
                row["end"] = stamp()
                row["wall_seconds"] = row["end"]["wall"] - row["start"]["wall"]
                row["cpu_seconds"] = row["end"]["thread_cpu"] - row["start"]["thread_cpu"]
                events.append(row)

        patch.setattr(owner, name, observed)

    gc.callbacks.append(observe_gc)
    try:
        start = stamp()
        events.append(
            {"name": "test_call_start", **start, "gc_counts": gc.get_count(), "gc_thresholds": gc.get_threshold()}
        )
        for owner, name, label in [
            (j1_fetch, "public_url", "public_url"),
            (j1_fetch, "_request_public", "request_public"),
            (j1_fetch, "_deadline_timeout", "deadline_timeout"),
            (j1_fetch, "_deadline_response_class", "response_class"),
            (j1_fetch._PinnedHTTP, "__init__", "HTTPinit"),
            (j1_fetch._PinnedHTTP, "request", "HTTPrequest"),
            (j1_fetch._PinnedHTTP, "getresponse", "HTTPgetresponse"),
            (j1_fetch._PinnedHTTP, "close", "HTTPclose"),
            (j1_fetch._DeadlineReader, "__init__", "reader_init"),
            (j1_fetch._DeadlineReader, "readinto", "raw_readinto"),
            (j1_fetch._DeadlineReader, "close", "reader_close"),
        ]:
            wrap(owner, name, label)
        yield
    finally:
        events.append({"name": "test_call_end", **stamp()})
        gc.callbacks.remove(observe_gc)
        patch.undo()
        terminal = item.config.pluginmanager.getplugin("terminalreporter")
        terminal.write_line("R2_HTTP_DIAGNOSTIC=" + json.dumps({"nodeid": item.nodeid, "events": events}))
