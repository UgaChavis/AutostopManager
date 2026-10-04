"""Keep an E4 collector bounded by its owning MCP HTTP request."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

_T = TypeVar("_T")


async def _wait_for_disconnect(request: Any) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(0.05)


async def run_identity_request(operation: Awaitable[_T], context: Any = None) -> _T:
    """Cancel provider work when the HTTP caller leaves or the tool is cancelled.

    Direct library calls have no HTTP context. Cancellation of those calls is
    still propagated to the collector without a background watcher.
    """
    if context is None:
        return await operation
    try:
        request = context.request_context.request
    except ValueError:
        return await operation
    if not callable(getattr(request, "is_disconnected", None)):
        return await operation
    work = asyncio.ensure_future(operation)
    disconnected = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait((work, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        disconnected.result()
        raise asyncio.CancelledError
    finally:
        for task in (work, disconnected):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, disconnected, return_exceptions=True)
