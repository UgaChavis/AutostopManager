from copy import deepcopy

import pytest

from scripts import crm_backup_reboot_review as review
from scripts import crm_backup_recovery_journal as journal


ATTEMPT = "a" * 32
OLD_BOOT = "b" * 32
NEW_BOOT = "c" * 32


class FakeReadback:
    def __init__(self, *results):
        self.results = iter(results)
        self.requests = []

    def read_all(self, names):
        self.requests.append(names)
        value = next(self.results)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


@pytest.fixture
def attempt(tmp_path, monkeypatch):
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
                    "name": "duty",
                    "identity": "duty-id",
                    "kind": "docker",
                    "was_active": False,
                    "step": "untouched",
                    "restart_policy": {"name": "no", "maximum_retry_count": 0},
                },
            ],
        )
        locked.policy_transition(
            attempt=ATTEMPT, current_boot=OLD_BOOT, name="crm", expected="untouched", new="change_intent"
        )
        monkeypatch.setattr(journal, "_current_boot", lambda: NEW_BOOT)
        yield locked


def test_active_docker_and_originally_disabled_service_never_trigger_actions(attempt):
    observations = {
        "crm": {
            "kind": "docker",
            "identity": "crm-id",
            "state": "active",
            "restart_policy": {"name": "always", "maximum_retry_count": 0},
        },
        "duty": {
            "kind": "docker",
            "identity": "duty-id",
            "state": "active",
            "restart_policy": {"name": "no", "maximum_retry_count": 0},
        },
    }
    backend = FakeReadback(observations, observations)
    result = review.review_after_reboot(attempt, backend, current_boot=NEW_BOOT)
    assert backend.requests == [("crm", "duty"), ("crm", "duty")]
    assert result["automatic_effects_allowed"] is False
    assert result["journal_completion_allowed"] is False
    assert result["docker_autostart_before_review"] == "uncontrolled"
    assert result["services"][0]["journal_step"] == "untouched"
    assert result["services"][0]["policy_step"] == "change_intent"
    assert result["services"][0]["policy_action"] == "inspect_after_boot"
    assert result["services"][0]["kind_matches_recorded"] is True
    assert result["services"][0]["observed_restart_policy"] == {"name": "always", "maximum_retry_count": 0}
    assert result["services"][0]["policy_matches_recorded"] is True
    assert result["services"][1]["was_active"] is False
    assert result["services"][1]["state"] == "active"
    assert all(item["ownership"] == "unproven" for item in result["services"])
    assert all(item["restart_policy_ownership"] == "unproven" for item in result["services"])


def test_changed_identity_or_policy_and_unknown_service_are_not_confirmed(attempt):
    initial = {
        "crm": {
            "kind": "docker",
            "identity": "crm-id",
            "state": "inactive",
            "restart_policy": {"name": "no", "maximum_retry_count": 0},
        },
        "duty": {"kind": "docker", "identity": "duty-id", "state": "inactive", "restart_policy": "unknown"},
    }
    later = deepcopy(initial)
    later["crm"] = {
        "kind": "docker",
        "identity": "other-id",
        "state": "active",
        "restart_policy": {"name": "no", "maximum_retry_count": 0},
    }
    later["duty"]["identity"] = None
    result = review.review_after_reboot(attempt, FakeReadback(initial, later), current_boot=NEW_BOOT)
    crm, duty = result["services"]
    assert crm["identity_matches_recorded"] is False and not crm["observations_match"]
    assert crm["policy_matches_recorded"] is False and crm["policy_step"] == "change_intent"
    assert duty["identity_matches_recorded"] is None and not duty["observations_match"]
    assert duty["policy_matches_recorded"] is None
    assert result["automatic_effects_allowed"] is False


@pytest.mark.parametrize("invalid", [{}, {"crm": {}}, RuntimeError("sensitive backend detail")])
def test_missing_or_failed_readback_fails_closed_without_partial_report(attempt, invalid):
    with pytest.raises(review.RebootReviewError) as failure:
        review.review_after_reboot(attempt, FakeReadback(invalid), current_boot=NEW_BOOT)
    assert "sensitive backend detail" not in str(failure.value)


def test_same_boot_is_not_reboot_recovery(attempt, monkeypatch):
    monkeypatch.setattr(journal, "_current_boot", lambda: OLD_BOOT)
    backend = FakeReadback()
    with pytest.raises(review.RebootReviewError, match="not_applicable"):
        review.review_after_reboot(attempt, backend, current_boot=OLD_BOOT)
    assert backend.requests == []


def test_forged_boot_is_rejected_by_journal(attempt):
    backend = FakeReadback()
    with pytest.raises(journal.RecoveryJournalError, match="boot_mismatch"):
        review.review_after_reboot(attempt, backend, current_boot=OLD_BOOT)
    assert backend.requests == []


def test_exact_retry_count_is_included_in_policy_comparison(attempt):
    first = {
        "crm": {
            "kind": "docker",
            "identity": "crm-id",
            "state": "inactive",
            "restart_policy": {"name": "on-failure", "maximum_retry_count": 1},
        },
        "duty": {
            "kind": "docker",
            "identity": "duty-id",
            "state": "inactive",
            "restart_policy": {"name": "no", "maximum_retry_count": 0},
        },
    }
    second = deepcopy(first)
    second["crm"]["restart_policy"]["maximum_retry_count"] = 2
    result = review.review_after_reboot(attempt, FakeReadback(first, second), current_boot=NEW_BOOT)
    assert result["services"][0]["observations_match"] is False
    assert result["services"][0]["policy_matches_recorded"] is False
    assert result["services"][1]["policy_matches_recorded"] is True
    assert result["automatic_effects_allowed"] is False


def test_same_policy_name_with_different_retry_count_does_not_match(tmp_path, monkeypatch):
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
                    "restart_policy": {"name": "on-failure", "maximum_retry_count": 1},
                }
            ],
        )
        monkeypatch.setattr(journal, "_current_boot", lambda: NEW_BOOT)
        observed = {
            "crm": {
                "kind": "docker",
                "identity": "crm-id",
                "state": "inactive",
                "restart_policy": {"name": "on-failure", "maximum_retry_count": 2},
            }
        }
        result = review.review_after_reboot(locked, FakeReadback(observed, observed), current_boot=NEW_BOOT)
        assert result["services"][0]["observations_match"] is True
        assert result["services"][0]["policy_matches_recorded"] is False
        assert result["automatic_effects_allowed"] is False


def test_reboot_after_policy_change_and_stop_intent_remains_unresolved(tmp_path, monkeypatch):
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
                }
            ],
        )
        locked.policy_transition(
            attempt=ATTEMPT, current_boot=OLD_BOOT, name="crm", expected="untouched", new="change_intent"
        )
        locked.policy_transition(
            attempt=ATTEMPT, current_boot=OLD_BOOT, name="crm", expected="change_intent", new="changed"
        )
        locked.transition(attempt=ATTEMPT, current_boot=OLD_BOOT, name="crm", expected="untouched", new="stop_intent")
        assert locked.recovery_plan(current_boot=OLD_BOOT)["services"][0]["policy_action"] == "wait_for_service_restore"
        monkeypatch.setattr(journal, "_current_boot", lambda: NEW_BOOT)
        observed = {
            "crm": {
                "kind": "docker",
                "identity": "crm-id",
                "state": "inactive",
                "restart_policy": {"name": "no", "maximum_retry_count": 0},
            }
        }
        result = review.review_after_reboot(locked, FakeReadback(observed, observed), current_boot=NEW_BOOT)
        assert result["services"][0]["policy_step"] == "changed"
        assert result["services"][0]["policy_action"] == "inspect_after_boot"
        assert result["services"][0]["journal_step"] == "stop_intent"
        assert result["services"][0]["policy_matches_recorded"] is False
        assert result["automatic_effects_allowed"] is False
        assert result["journal_completion_allowed"] is False


def test_changed_resource_kind_is_not_treated_as_identity_proof(attempt):
    observations = {
        "crm": {"kind": "systemd", "identity": "crm-id", "state": "active", "restart_policy": None},
        "duty": {"kind": "unknown", "identity": None, "state": "unknown", "restart_policy": "unknown"},
    }
    result = review.review_after_reboot(attempt, FakeReadback(observations, observations), current_boot=NEW_BOOT)
    assert result["services"][0]["identity_matches_recorded"] is True
    assert result["services"][0]["kind_matches_recorded"] is False
    assert result["services"][1]["kind_matches_recorded"] is None
    assert all(item["ownership"] == "unproven" for item in result["services"])
    assert result["automatic_effects_allowed"] is False
