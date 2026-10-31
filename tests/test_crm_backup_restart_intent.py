import pytest

from scripts import crm_backup_recovery_journal as journal

ATTEMPT = "a" * 32
BOOT = "b" * 32


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "_current_boot", lambda: BOOT)
    path = tmp_path / "recovery"
    path.mkdir(mode=0o700)
    return path


def begin(store, *, active=True, name="always"):
    store.begin(
        attempt=ATTEMPT,
        boot=BOOT,
        services=[
            {
                "name": "crm",
                "kind": "docker",
                "identity": "container-id",
                "was_active": active,
                "step": "untouched",
                "restart_policy": {"name": name, "maximum_retry_count": 0},
            }
        ],
    )


def policy(store, expected, new):
    store.policy_transition(attempt=ATTEMPT, current_boot=BOOT, name="crm", expected=expected, new=new)


def service(store, expected, new):
    store.transition(attempt=ATTEMPT, current_boot=BOOT, name="crm", expected=expected, new=new)


def test_policy_intent_and_durable_readback_precede_stop_and_restore(root):
    with journal.locked_journal(root) as store:
        begin(store)
        with pytest.raises(journal.RecoveryJournalError, match="policy_not_fenced"):
            service(store, "untouched", "stop_intent")
        policy(store, "untouched", "change_intent")
    with journal.locked_journal(root) as store:
        result = store.recovery_plan(current_boot=BOOT)["services"][0]
        assert result["restart_policy"] == {"name": "always", "maximum_retry_count": 0}
        assert result["policy_step"] == "change_intent"
        assert result["policy_action"] == "inspect_policy_change_outcome"
        with pytest.raises(journal.RecoveryJournalError, match="policy_not_fenced"):
            service(store, "untouched", "stop_intent")
        policy(store, "change_intent", "changed")
        assert (
            store.recovery_plan(current_boot=BOOT)["services"][0]["policy_action"]
            == "verify_ownership_then_restore_policy"
        )
        service(store, "untouched", "stop_intent")
        service(store, "stop_intent", "stopped")
        assert store.recovery_plan(current_boot=BOOT)["services"][0]["policy_action"] == "wait_for_service_restore"
        with pytest.raises(journal.RecoveryJournalError, match="service_not_restored"):
            policy(store, "changed", "restore_intent")
        service(store, "stopped", "restore_intent")
        service(store, "restore_intent", "restored")
        with pytest.raises(journal.RecoveryJournalError, match="incomplete"):
            store.complete(attempt=ATTEMPT, current_boot=BOOT)
        policy(store, "changed", "restore_intent")
        policy(store, "restore_intent", "restored")
        store.complete(attempt=ATTEMPT, current_boot=BOOT)


def test_failed_policy_change_requires_inspection_and_can_reconcile_unchanged(root):
    with journal.locked_journal(root) as store:
        begin(store)
        policy(store, "untouched", "change_intent")
        with pytest.raises(journal.RecoveryJournalError, match="incomplete"):
            store.complete(attempt=ATTEMPT, current_boot=BOOT)
        policy(store, "change_intent", "restored")
        store.complete(attempt=ATTEMPT, current_boot=BOOT)


def test_inactive_service_and_already_disabled_policy_cannot_be_changed(root):
    with journal.locked_journal(root) as store:
        with pytest.raises(journal.RecoveryJournalError, match="inactive_autostart_unfenced"):
            begin(store, active=False)
        store.begin(
            attempt="c" * 32,
            boot=BOOT,
            services=[
                {
                    "name": "crm",
                    "kind": "docker",
                    "identity": "id",
                    "was_active": True,
                    "step": "untouched",
                    "restart_policy": {"name": "no", "maximum_retry_count": 0},
                }
            ],
        )
        with pytest.raises(journal.RecoveryJournalError, match="transition_invalid"):
            store.policy_transition(
                attempt="c" * 32, current_boot=BOOT, name="crm", expected="untouched", new="change_intent"
            )


def test_missing_docker_policy_and_duplicate_identity_fail_before_attempt(root):
    with journal.locked_journal(root) as store:
        invalid = {
            "name": "crm",
            "kind": "docker",
            "identity": "id",
            "was_active": True,
            "step": "untouched",
            "restart_policy": None,
        }
        with pytest.raises(journal.RecoveryJournalError, match="state_invalid"):
            store.begin(attempt=ATTEMPT, boot=BOOT, services=[invalid])
        first = {
            "name": "one",
            "kind": "docker",
            "identity": "id",
            "was_active": True,
            "step": "untouched",
            "restart_policy": {"name": "no", "maximum_retry_count": 0},
        }
        second = {**first, "name": "alias"}
        with pytest.raises(journal.RecoveryJournalError, match="state_invalid"):
            store.begin(attempt=ATTEMPT, boot=BOOT, services=[first, second])
        assert store.read() is None


def test_inactive_systemd_without_early_boot_fence_rejects_attempt(root):
    with journal.locked_journal(root) as store:
        unit = {
            "name": "unit",
            "kind": "systemd",
            "identity": "id",
            "was_active": False,
            "step": "untouched",
            "restart_policy": None,
        }
        with pytest.raises(journal.RecoveryJournalError, match="inactive_systemd_unfenced"):
            store.begin(attempt=ATTEMPT, boot=BOOT, services=[unit])
        assert store.read() is None


def test_inconsistent_policy_and_stop_state_is_rejected(root):
    with journal.locked_journal(root) as store:
        begin(store)
        state = store.read()
        state["services"][0]["step"] = "stopped"
        with pytest.raises(journal.RecoveryJournalError, match="state_invalid"):
            store._save(state)
        assert store.read()["services"][0]["step"] == "untouched"


def test_older_journal_is_not_discarded_or_implicitly_upgraded(root):
    path = root / "crm-recovery.json"
    original = b'{"schema":2,"complete":false}'
    path.write_bytes(original)
    path.chmod(0o600)
    with pytest.raises(journal.RecoveryJournalError, match="state_invalid"):
        with journal.locked_journal(root):
            pass
    assert path.read_bytes() == original


def test_new_boot_requires_manual_inspection_and_never_changes_policy(root, monkeypatch):
    with journal.locked_journal(root) as store:
        begin(store)
        policy(store, "untouched", "change_intent")
        monkeypatch.setattr(journal, "_current_boot", lambda: "d" * 32)
        assert store.recovery_plan(current_boot="d" * 32)["services"][0]["action"] == "inspect_after_boot"
        with pytest.raises(journal.RecoveryJournalError, match="boot_mismatch"):
            store.policy_transition(
                attempt=ATTEMPT, current_boot="d" * 32, name="crm", expected="change_intent", new="changed"
            )
