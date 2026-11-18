"""Bounded, process-local timing aggregates for Telegram diagnostics."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any


class StageTimings:
    """Bounded, process-local observations; returned awaits are not business success."""

    MODES = ("new", "recovery")
    STAGES = ("peer_prepare", "idle_wait", "turn_lifecycle", "finish_request")

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        stages: tuple[str, ...] | Mapping[str, tuple[str, ...]] = STAGES,
        modes: tuple[str, ...] = MODES,
    ) -> None:
        self._clock = clock
        self._since = self.timestamp()
        self._dropped = 0
        self._cells = {
            mode: {
                stage: {
                    "count": 0,
                    "sum_seconds": 0.0,
                    "max_seconds": 0.0,
                    "returned": 0,
                    "raised": 0,
                    "cancelled": 0,
                    "in_flight": 0,
                    **({"unsettled": 0} if stage == "finish_request" else {}),
                }
                for stage in (stages[mode] if isinstance(stages, Mapping) else stages)
            }
            for mode in modes
        }

    def timestamp(self) -> float | None:
        """Read the local monotonic clock without affecting the observed operation."""

        try:
            value = float(self._clock())
            return value if math.isfinite(value) else None
        except Exception:  # noqa: BLE001 - measurement failures never replace execution outcomes.
            return None

    def _record(
        self,
        cell: dict[str, Any],
        outcome: str,
        started: float | None,
        ended: float | None,
        unsettled: Callable[[], bool] | None,
    ) -> None:
        if started is None or ended is None or outcome not in {"returned", "raised", "cancelled"}:
            raise ValueError
        elapsed = ended - started
        total = cell["sum_seconds"] + elapsed
        if elapsed < 0 or not math.isfinite(elapsed) or not math.isfinite(total):
            raise ValueError
        unfinished = outcome == "returned" and unsettled is not None and unsettled()
        cell["count"] += 1
        cell["sum_seconds"] = total
        cell["max_seconds"] = max(cell["max_seconds"], elapsed)
        cell[outcome] += 1
        if unfinished:
            cell["unsettled"] += 1

    def record(
        self, mode: str, stage: str, started: float | None, ended: float | None, *, outcome: str = "returned"
    ) -> None:
        """Record an observation whose boundaries do not fit in a single await."""

        try:
            self._record(self._cells[mode][stage], outcome, started, ended, None)
        except Exception:  # noqa: BLE001 - no exception payload is retained or exported.
            self._dropped += 1

    @contextmanager
    def measure(self, mode: str, stage: str, *, unsettled: Callable[[], bool] | None = None) -> Iterator[None]:
        cell = self._cells[mode][stage]
        started = self.timestamp()
        cell["in_flight"] += 1
        outcome = "raised"
        try:
            yield
            outcome = "returned"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            cell["in_flight"] -= 1
            try:
                self._record(cell, outcome, started, self.timestamp(), unsettled)
            except Exception:  # noqa: BLE001 - no exception payload is retained or exported.
                self._dropped += 1

    def snapshot(self) -> dict[str, Any]:
        now = self.timestamp()
        age = None if now is None or self._since is None else now - self._since
        return {
            "scope": "process",
            "observed_seconds": age if age is not None and math.isfinite(age) and age >= 0 else None,
            "dropped_samples": self._dropped,
            **{
                mode: {
                    stage: {
                        **cell,
                        "sum_seconds": cell["sum_seconds"] if cell["count"] else None,
                        "max_seconds": cell["max_seconds"] if cell["count"] else None,
                    }
                    for stage, cell in cells.items()
                }
                for mode, cells in self._cells.items()
            },
        }
