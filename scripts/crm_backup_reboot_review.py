"""Read-only post-reboot review; never start, stop, or reconfigure a service.

The journal can record the original Docker restart policy and policy-change
intent; neither proves that the effect happened or that its ownership persists.
Two equal observations are not an atomic snapshot or a fence against later
changes. No result authorizes effects or completion of the previous attempt.
A real backend and boot ordering are deliberately not supplied by this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from .crm_backup_recovery_journal import RecoveryJournal


class RebootReviewError(RuntimeError):
    pass


class Readback(Protocol):
    def read_all(self, names: tuple[str, ...]) -> Mapping[str, Mapping[str, Any]]:
        """Return a read-only observation for every requested service."""


_IDENTITY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@-]{0,127}\Z")
_STATES = {"active", "inactive", "starting", "stopping", "failed", "unknown"}
_POLICIES = {"no", "always", "unless-stopped", "on-failure"}


def _policy(value: Any) -> dict[str, Any] | str | None:
    if value is None or value == "unknown":
        return value
    if not isinstance(value, Mapping) or set(value) != {"name", "maximum_retry_count"}:
        raise RebootReviewError("reboot_readback_invalid")
    name, count = value["name"], value["maximum_retry_count"]
    if (
        not isinstance(name, str)
        or name not in _POLICIES
        or type(count) is not int
        or not 0 <= count <= 2**31 - 1
        or (name != "on-failure" and count != 0)
    ):
        raise RebootReviewError("reboot_readback_invalid")
    return {"name": name, "maximum_retry_count": count}


def _read_all(backend: Readback, names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    try:
        values = backend.read_all(names)
        if not isinstance(values, Mapping) or set(values) != set(names):
            raise RebootReviewError("reboot_readback_incomplete")
        result = {}
        for name in names:
            row = values[name]
            if not isinstance(row, Mapping) or set(row) != {"kind", "identity", "state", "restart_policy"}:
                raise RebootReviewError("reboot_readback_invalid")
            kind, identity, state, policy = row["kind"], row["identity"], row["state"], _policy(row["restart_policy"])
            if (
                not isinstance(kind, str)
                or kind not in {"docker", "systemd", "unknown"}
                or (kind == "docker" and policy is None)
                or (kind == "systemd" and policy not in (None, "unknown"))
                or (kind == "unknown" and policy != "unknown")
                or (identity is not None and (not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)))
                or not isinstance(state, str)
                or state not in _STATES
            ):
                raise RebootReviewError("reboot_readback_invalid")
            result[name] = {"kind": kind, "identity": identity, "state": state, "restart_policy": policy}
        return result
    except Exception:  # noqa: BLE001 - do not expose arbitrary backend errors.
        # Backend errors may contain service output or sensitive data.
        raise RebootReviewError("reboot_readback_unavailable") from None


def review_after_reboot(journal: RecoveryJournal, backend: Readback, *, current_boot: str) -> dict[str, Any]:
    """Report two observations without authorizing any action on the services."""
    before = journal.read()
    plan = journal.recovery_plan(current_boot=current_boot)
    if before is None or before["complete"] or not plan.get("boot_changed"):
        raise RebootReviewError("reboot_review_not_applicable")
    names = tuple(item["name"] for item in before["services"])
    first = _read_all(backend, names)
    second = _read_all(backend, names)
    if journal.read() != before:
        raise RebootReviewError("reboot_journal_changed")
    # Revalidate the actual boot and journal ownership after all backend reads.
    journal.recovery_plan(current_boot=current_boot)

    services = []
    planned = {item["name"]: item for item in plan["services"]}
    for saved in before["services"]:
        name = saved["name"]
        current = second[name]
        identity_match = None if current["identity"] is None else current["identity"] == saved["identity"]
        policy_match = (
            None if current["restart_policy"] == "unknown" else current["restart_policy"] == saved["restart_policy"]
        )
        services.append(
            {
                "name": name,
                "recorded_kind": saved["kind"],
                "observed_kind": current["kind"],
                "kind_matches_recorded": None if current["kind"] == "unknown" else current["kind"] == saved["kind"],
                "was_active": saved["was_active"],
                "journal_step": saved["step"],
                "policy_action": planned[name]["policy_action"],
                "state": current["state"],
                "identity_matches_recorded": identity_match,
                "original_restart_policy": saved["restart_policy"],
                "policy_step": saved["policy_step"],
                "observed_restart_policy": current["restart_policy"],
                "policy_matches_recorded": policy_match,
                "observations_match": first[name] == current,
                "ownership": "unproven",
                "restart_policy_ownership": "unproven",
            }
        )
    return {
        "attempt": before["attempt"],
        "status": "manual_review_required",
        "automatic_effects_allowed": False,
        "journal_completion_allowed": False,
        "docker_autostart_before_review": "uncontrolled",
        "services": services,
    }
