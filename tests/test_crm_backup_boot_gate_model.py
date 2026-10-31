"""Synthetic interruption model: this does not test systemd or Docker ordering."""

from copy import deepcopy

import pytest

from scripts import crm_backup_reboot_review as review
from scripts import crm_backup_recovery_journal as journal


OLD_BOOT = "a" * 32
NEW_BOOT = "b" * 32
ATTEMPT = "c" * 32


class FakeReadback:
    def __init__(self, observed):
        self.observed = observed

    def read_all(self, names):
        assert names == ("crm", "previously_off")
        return deepcopy(self.observed)


@pytest.mark.parametrize(
    "crash_after",
    [
        "initial",
        "policy_intent",
        "policy_changed",
        "stop_intent",
        "stopped",
        "service_restored",
        "policy_restore_intent",
    ],
)
@pytest.mark.parametrize("observed_policy", ["no", "always"])
def test_reboot_at_each_policy_and_stop_boundary_requires_review(tmp_path, monkeypatch, crash_after, observed_policy):
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(journal, "_current_boot", lambda: OLD_BOOT)
    with journal.locked_journal(root) as locked:
        locked.begin(
            attempt=ATTEMPT,
            boot=OLD_BOOT,
            services=[
                {
                    "name": "crm",
                    "identity": "crm-id",
                    "kind": "docker",
                    "was_active": True,
                    "step": "untouched",
                    "restart_policy": {"name": "always", "maximum_retry_count": 0},
                },
                {
                    "name": "previously_off",
                    "identity": "off-id",
                    "kind": "docker",
                    "was_active": False,
                    "step": "untouched",
                    "restart_policy": {"name": "no", "maximum_retry_count": 0},
                },
            ],
        )
        stages = [
            ("policy_intent", "policy", "untouched", "change_intent"),
            ("policy_changed", "policy", "change_intent", "changed"),
            ("stop_intent", "service", "untouched", "stop_intent"),
            ("stopped", "service", "stop_intent", "stopped"),
            ("service_restore_intent", "service", "stopped", "restore_intent"),
            ("service_restored", "service", "restore_intent", "restored"),
            ("policy_restore_intent", "policy", "changed", "restore_intent"),
        ]
        for stage, kind, expected, new in stages:
            if crash_after == "initial":
                break
            transition = locked.policy_transition if kind == "policy" else locked.transition
            transition(attempt=ATTEMPT, current_boot=OLD_BOOT, name="crm", expected=expected, new=new)
            if stage == crash_after:
                break
        old_state = locked.read()
        monkeypatch.setattr(journal, "_current_boot", lambda: NEW_BOOT)
        observed = {
            "crm": {
                "kind": "docker",
                "identity": "crm-id",
                "state": "active",
                "restart_policy": {"name": observed_policy, "maximum_retry_count": 0},
            },
            "previously_off": {
                "kind": "docker",
                "identity": "off-id",
                "state": "inactive",
                "restart_policy": {"name": "no", "maximum_retry_count": 0},
            },
        }
        result = review.review_after_reboot(locked, FakeReadback(observed), current_boot=NEW_BOOT)
        assert result["automatic_effects_allowed"] is False
        assert result["journal_completion_allowed"] is False
        assert result["docker_autostart_before_review"] == "uncontrolled"
        assert all(item["policy_action"] == "inspect_after_boot" for item in result["services"])
        assert result["services"][1]["was_active"] is False
        assert locked.read() == old_state
        with pytest.raises(journal.RecoveryJournalError, match="boot_mismatch"):
            locked.complete(attempt=ATTEMPT, current_boot=NEW_BOOT)
