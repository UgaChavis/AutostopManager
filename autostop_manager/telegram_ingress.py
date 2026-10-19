"""A process-local Telegram cut; its audit file never grants admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, cast

from .cold_runtime import ColdBinding, ColdRuntimeDenied


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _process_identity() -> tuple[int, int]:
    # Field 22 follows the parenthesized comm, which may contain spaces.
    raw = Path("/proc/self/stat").read_text(encoding="ascii")
    start = int(raw[raw.rfind(")") + 2 :].split()[19])
    return os.getpid(), start


def _publish(path: Path, document: dict[str, Any]) -> str:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    pending = path.with_name("." + path.name + ".pending")
    created = False
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if created:
            pending.unlink(missing_ok=True)
    return raw


class ColdIngressQuarantine:
    """Bounded technical observations, with overflow explicit and no replay API."""

    def __init__(self, path: Path, context: dict[str, Any]) -> None:
        self.path = path
        self.context = context
        self.records: dict[str, dict[str, Any]] = {}
        self.overflow_count = 0
        self.overflow_chain = "0" * 64
        self._save()

    def _save(self) -> None:
        _publish(
            self.path,
            {
                **self.context,
                "schema": "autostop.manager.cold-telegram-ingress-quarantine.v1",
                "status": "needs_review" if self.records or self.overflow_count else "clear",
                "records": list(self.records.values()),
                "overflow_count": self.overflow_count,
                "overflow_chain_sha256": self.overflow_chain,
                "complete_observation_log": self.overflow_count == 0,
                "historical_inventory_complete": False,
                "replay": "forbidden",
                "observation_only": True,
                "release_authority": False,
            },
        )

    def hold(self, event: Any, *, source: str, reason: str) -> None:
        def integer(value: Any) -> int | None:
            return value if type(value) is int and -(2**63) < value < 2**63 else None

        identity = {
            "source": source,
            "peer": integer(getattr(event, "chat_id", None)),
            "message": integer(getattr(event, "id", None)),
        }
        stamp = getattr(event, "date", None)
        if stamp is None:
            stamp = getattr(getattr(event, "message", None), "date", None)
        metadata = {
            **identity,
            "pts": integer(getattr(getattr(event, "original_update", None), "pts", None)),
            "date": stamp.isoformat() if isinstance(stamp, datetime) and stamp.tzinfo is not None else None,
            "reason": reason,
        }

        def digest(value: Any) -> str:
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        fingerprint = digest(metadata)
        if fingerprint in self.records:
            return
        if len(self.records) < 256:
            self.records[fingerprint] = {
                "locator": {
                    "role": self.context["role"],
                    "peer_id": identity["peer"],
                    "message_id": identity["message"],
                    "pts": metadata["pts"],
                },
                "source_identity_sha256": digest(identity),
                "observed_record_sha256": fingerprint,
                "source": source,
                "reason": reason,
                "status": "needs_review",
            }
        else:
            self.overflow_count += 1
            self.overflow_chain = hashlib.sha256((self.overflow_chain + fingerprint).encode("ascii")).hexdigest()
        self._save()


@dataclass(frozen=True)
class ColdIngressCut:
    pts: int
    receipt_json: str
    quarantine: ColdIngressQuarantine

    def accepts(self, event: Any) -> bool:
        """Only account-global private updates carry the accepted counter."""
        update = getattr(event, "original_update", None)
        pts = getattr(update, "pts", None)
        stamp = getattr(event, "date", None)
        if stamp is None:
            stamp = getattr(getattr(event, "message", None), "date", None)
        return (
            getattr(event, "is_private", None) is True
            and not bool(getattr(event, "out", False))
            and _positive(getattr(event, "chat_id", None))
            and _positive(getattr(event, "id", None))
            and type(pts) is int
            and self.pts < pts <= 0x7FFFFFFF
            and isinstance(stamp, datetime)
            and stamp.tzinfo is not None
            and stamp.utcoffset() is not None
        )


async def capture_cold_ingress(
    client: Any, current: ColdBinding, state_dir: Path, state_request: Any
) -> ColdIngressCut:
    """Called once by the authenticated owned daemon before registering intake.

    Missing metadata aborts startup. Restarts take a new cut; neither the old
    audit nor historical messages can lower the cut or recreate old jobs.
    """
    if current.role not in {"work-telegram", "personal-telegram"}:
        raise ColdRuntimeDenied("cold_ingress_role_invalid")
    account = await client.get_me()
    account_id = getattr(account, "id", None)
    if not _positive(account_id) or getattr(account, "is_self", None) is not True:
        raise ColdRuntimeDenied("cold_ingress_account_unknown")
    state = await client(state_request)
    values = {name: getattr(state, name, None) for name in ("pts", "qts", "seq")}
    if any(type(value) is not int or not 0 <= value <= 0x7FFFFFFF for value in values.values()):
        raise ColdRuntimeDenied("cold_ingress_state_unknown")
    pid, start = _process_identity()
    if not _positive(pid) or not _positive(start):
        raise ColdRuntimeDenied("cold_ingress_process_unknown")
    document = {
        "schema": "autostop.manager.cold-telegram-ingress-cut.v1",
        **{
            name: current.document[name]
            for name in ("attempt_id", "boot_id", "generation", "epoch_id", "source_revision")
        },
        "role": current.role,
        "main_pid": pid,
        "start_ticks": start,
        "observed_monotonic_ns": time.monotonic_ns(),
        "account_self_sha256": hashlib.sha256(str(account_id).encode("ascii")).hexdigest(),
        **values,
        "supported_updates": "private_account_global_pts_only",
        "historical_replay": "denied",
        "observation_only": True,
        "release_authority": False,
    }
    quarantine_path = state_dir / f"cold-ingress-quarantine-{pid}-{start}.json"
    quarantine = ColdIngressQuarantine(quarantine_path, dict(document))
    document["quarantine_path"] = str(quarantine_path)
    raw = _publish(state_dir / "cold-ingress-cut.json", document)
    return ColdIngressCut(pts=cast(int, values["pts"]), receipt_json=raw, quarantine=quarantine)
