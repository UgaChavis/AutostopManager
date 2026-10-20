"""Small numeric coercions shared by isolated stdlib-only workers."""

from __future__ import annotations

from typing import Any


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(numeric, maximum))
