"""Small, fail-closed runtime policy readers for owner-controlled routes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT


MANAGER_RULES_PATH = PROJECT_ROOT / "docs" / "agent" / "manager_rules.json"
STORE_POLICY_KEY = "autostop_store"
STORE_POLICY_CHANGE_CONTROL = "explicit_owner_reauthorization"
STORE_POLICY_STATES = frozenset({"paused", "enabled"})


@dataclass(frozen=True)
class StoreAccessPolicy:
    """The current Store access state, read fresh from the canonical policy file."""

    state: str
    valid: bool
    reason: str
    source: str

    @property
    def paused(self) -> bool:
        return self.state != "enabled"


def get_store_access_policy(*, path: Path | None = None) -> StoreAccessPolicy:
    """Read the Store policy without caching so an authorized change takes effect immediately.

    A missing, malformed, or incomplete policy remains paused.  The only
    re-enabling value is ``state=enabled`` under the documented owner-change
    control; a natural-language query never changes this state.
    """

    policy_path = path or MANAGER_RULES_PATH
    source = str(policy_path)
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _paused_policy("policy_unavailable", source)
    if not isinstance(payload, dict):
        return _paused_policy("policy_invalid_structure", source)

    policies = payload.get("runtime_policies")
    if not isinstance(policies, dict):
        return _paused_policy("policy_missing", source)
    configured = policies.get(STORE_POLICY_KEY)
    if not isinstance(configured, dict):
        return _paused_policy("store_policy_missing", source)

    state = str(configured.get("state") or "").casefold()
    change_control = str(configured.get("change_control") or "").casefold()
    if change_control != STORE_POLICY_CHANGE_CONTROL:
        return _paused_policy("store_policy_change_control_invalid", source)
    if state not in STORE_POLICY_STATES:
        return _paused_policy("store_policy_state_invalid", source)
    return StoreAccessPolicy(state=state, valid=True, reason="configured", source=source)


def store_access_is_paused() -> bool:
    """Return the fail-closed Store policy state for routing and agent briefs."""

    return get_store_access_policy().paused


def store_access_block(operation: str) -> dict[str, object] | None:
    """Return a compact fail-closed denial before a Store execution boundary."""

    policy = get_store_access_policy()
    if not policy.paused:
        return None
    return {
        "ok": False,
        "status": "blocked",
        "error": {"code": "store_access_paused"},
        "summary": {
            "operation": str(operation or "store"),
            "runtime_policy": "autostop_store",
            "policy_state": policy.state,
        },
    }


def _paused_policy(reason: str, source: str) -> StoreAccessPolicy:
    return StoreAccessPolicy(state="paused", valid=False, reason=reason, source=source)
