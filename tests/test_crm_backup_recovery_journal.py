import fcntl
import multiprocessing
import os
import select
import signal
import threading
from contextlib import suppress

import pytest

from scripts import crm_backup_recovery_journal as journal


ATTEMPT = "a" * 32
BOOT = "b" * 32
OTHER_BOOT = "c" * 32


@pytest.fixture(autouse=True)
def synthetic_boot(monkeypatch):
    monkeypatch.setattr(journal, "_current_boot", lambda: BOOT)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "recovery"
    path.mkdir(mode=0o700)
    return path


def begin(locked):
    locked.begin(
        attempt=ATTEMPT,
        boot=BOOT,
        services=[
            {"name": "running", "identity": "id-one", "was_active": True, "step": "untouched"},
            {"name": "already-off", "identity": "id-two", "was_active": False, "step": "untouched"},
        ],
    )


def transition(locked, expected, new, name="running"):
    locked.transition(attempt=ATTEMPT, current_boot=BOOT, name=name, expected=expected, new=new)


def test_interrupted_attempt_retains_original_state_and_recovery_steps(root, monkeypatch):
    with journal.locked_journal(root) as locked:
        begin(locked)
        transition(locked, "untouched", "stop_intent")
    with journal.locked_journal(root) as locked:
        monkeypatch.setattr(journal, "_current_boot", lambda: OTHER_BOOT)
        result = locked.recovery_plan(current_boot=OTHER_BOOT)
        assert result["boot_changed"] is True and result["status"] == "incomplete"
        assert [item["action"] for item in result["services"]] == ["inspect_after_boot", "inspect_after_boot"]
        monkeypatch.setattr(journal, "_current_boot", lambda: BOOT)
        assert locked.recovery_plan(current_boot=BOOT)["services"][0]["action"] == "inspect_stop_outcome"
        with pytest.raises(journal.RecoveryJournalError, match="transition_conflict"):
            transition(locked, "untouched", "stop_intent", name="already-off")
        transition(locked, "stop_intent", "stopped")
        assert locked.recovery_plan(current_boot=BOOT)["services"][0]["action"] == "verify_ownership_then_restore"
        transition(locked, "stopped", "restore_intent")
    with journal.locked_journal(root) as locked:
        assert locked.recovery_plan(current_boot=BOOT)["services"][0]["action"] == "inspect_restore_outcome"
        transition(locked, "restore_intent", "restored")
        locked.complete(attempt=ATTEMPT, current_boot=BOOT)
        assert locked.recovery_plan(current_boot=BOOT)["status"] == "complete"


def test_unknown_or_incomplete_attempt_cannot_be_overwritten(root):
    with journal.locked_journal(root) as locked:
        begin(locked)
        transition(locked, "untouched", "stop_intent")
        with pytest.raises(journal.RecoveryJournalError, match="previous_incomplete"):
            begin(locked)
        with pytest.raises(journal.RecoveryJournalError, match="incomplete"):
            locked.complete(attempt=ATTEMPT, current_boot=BOOT)
        with pytest.raises(journal.RecoveryJournalError, match="attempt_mismatch"):
            locked.transition(
                attempt="d" * 32, current_boot=BOOT, name="running", expected="stop_intent", new="stopped"
            )


def test_failed_write_does_not_remove_original_and_leaves_temp(root, monkeypatch):
    with journal.locked_journal(root) as locked:
        begin(locked)

        def fail_replace(*args, **kwargs):
            raise OSError("synthetic interruption")

        monkeypatch.setattr(journal.os, "replace", fail_replace)
        with pytest.raises(OSError):
            transition(locked, "untouched", "stop_intent")
        with pytest.raises(journal.RecoveryJournalError, match="durability_unknown"):
            locked.read()
        assert (root / ".crm-recovery.tmp").exists()
        with pytest.raises(journal.RecoveryJournalError, match="durability_unknown"):
            transition(locked, "untouched", "stop_intent")
        with pytest.raises(journal.RecoveryJournalError, match="temp_present"):
            locked.confirm_durable()


def test_interruption_after_replace_requires_exact_readback(root, monkeypatch):
    with journal.locked_journal(root) as locked:
        begin(locked)
        original = journal.os.fsync

        def fail_directory(fd):
            if fd == locked.directory:
                raise OSError("synthetic directory sync failure")
            original(fd)

        monkeypatch.setattr(journal.os, "fsync", fail_directory)
        with pytest.raises(OSError):
            transition(locked, "untouched", "stop_intent")
        with pytest.raises(journal.RecoveryJournalError, match="durability_unknown"):
            locked.read()
        with pytest.raises(journal.RecoveryJournalError, match="durability_unknown"):
            locked.recovery_plan(current_boot=BOOT)
        monkeypatch.setattr(journal.os, "fsync", original)
        assert locked.confirm_durable()["services"][0]["step"] == "stop_intent"
        assert locked.recovery_plan(current_boot=BOOT)["services"][0]["action"] == "inspect_stop_outcome"


def test_unrelated_files_and_unsafe_journal_are_preserved(root):
    other = root / "other"
    other.write_bytes(b"preserved")
    with journal.locked_journal(root) as locked:
        begin(locked)
    state = root / "crm-recovery.json"
    state.write_bytes(b"corrupt")
    with pytest.raises(journal.RecoveryJournalError, match="state_invalid"):
        with journal.locked_journal(root):
            pass
    assert state.read_bytes() == b"corrupt"
    assert other.read_bytes() == b"preserved"


def _try_lock(path, connection):
    try:
        with journal.locked_journal(path):
            connection.send("acquired")
    except journal.RecoveryJournalError as error:
        connection.send(str(error))
    finally:
        connection.close()


def test_concurrent_attempt_does_not_wait(root):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    with journal.locked_journal(root):
        process = context.Process(target=_try_lock, args=(root, child))
        process.start()
        assert parent.poll(5) and parent.recv() == "recovery_busy"
    process.join(5)
    assert process.exitcode == 0


def test_fifo_state_is_rejected_without_wait(root):
    os.mkfifo(root / "crm-recovery.json", 0o600)
    with pytest.raises(journal.RecoveryJournalError, match="file_unsafe"):
        with journal.locked_journal(root):
            pass


def test_new_boot_requires_inspection_and_blocks_all_transitions(root, monkeypatch):
    with journal.locked_journal(root) as locked:
        begin(locked)
        transition(locked, "untouched", "stop_intent")
        monkeypatch.setattr(journal, "_current_boot", lambda: OTHER_BOOT)
        actions = locked.recovery_plan(current_boot=OTHER_BOOT)["services"]
        assert [item["was_active"] for item in actions] == [True, False]
        assert all(item["action"] == "inspect_after_boot" for item in actions)
        with pytest.raises(journal.RecoveryJournalError, match="boot_mismatch"):
            locked.transition(
                attempt=ATTEMPT, current_boot=OTHER_BOOT, name="running", expected="stop_intent", new="stopped"
            )
        with pytest.raises(journal.RecoveryJournalError, match="boot_mismatch"):
            locked.complete(attempt=ATTEMPT, current_boot=OTHER_BOOT)
        with pytest.raises(journal.RecoveryJournalError, match="boot_mismatch"):
            locked.transition(attempt=ATTEMPT, current_boot=BOOT, name="running", expected="stop_intent", new="stopped")


def test_attempt_ids_cannot_be_reused_after_later_attempts(root):
    with journal.locked_journal(root) as locked:
        begin(locked)
        locked.complete(attempt=ATTEMPT, current_boot=BOOT)
        services = [{"name": "running", "identity": "id-one", "was_active": True, "step": "untouched"}]
        locked.begin(attempt="d" * 32, boot=BOOT, services=services)
        locked.complete(attempt="d" * 32, current_boot=BOOT)
        with pytest.raises(journal.RecoveryJournalError, match="attempt_reused"):
            locked.begin(attempt=ATTEMPT, boot=BOOT, services=services)


def test_replaced_lock_or_directory_rejects_further_actions(root):
    with journal.locked_journal(root) as locked:
        begin(locked)
        (root / ".crm-recovery.lock").rename(root / ".old-lock")
        (root / ".crm-recovery.lock").touch(mode=0o600)
        with pytest.raises(journal.RecoveryJournalError, match="lock_changed"):
            locked.read()
    (root / ".crm-recovery.lock").unlink()
    (root / ".old-lock").rename(root / ".crm-recovery.lock")
    with journal.locked_journal(root) as locked:
        root.rename(root.with_name("moved"))
        root.mkdir(mode=0o700)
        with pytest.raises(journal.RecoveryJournalError, match="directory_changed"):
            locked.read()


def test_threads_serialize_complete_transition_transaction(root, monkeypatch):
    with journal.locked_journal(root) as locked:
        locked.begin(
            attempt=ATTEMPT,
            boot=BOOT,
            services=[
                {"name": "a", "identity": "a", "was_active": True, "step": "untouched"},
                {"name": "b", "identity": "b", "was_active": True, "step": "untouched"},
            ],
        )
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        original = locked._save
        errors = []

        def intercepted(state):
            if threading.current_thread().name == "first":
                entered.set()
                assert release.wait(5)
            else:
                second_entered.set()
            original(state)

        def run(name):
            try:
                transition(locked, "untouched", "stop_intent", name=name)
            except BaseException as error:  # noqa: BLE001 - relay failures from a worker thread.
                errors.append(error)

        monkeypatch.setattr(locked, "_save", intercepted)
        first = threading.Thread(target=run, args=("a",), name="first")
        second = threading.Thread(target=run, args=("b",), name="second")
        first.start()
        assert entered.wait(5)
        second.start()
        try:
            assert not second_entered.wait(0.1)
        finally:
            release.set()
            first.join(5)
            second.join(5)
        assert not first.is_alive() and not second.is_alive() and not errors
        assert [item["step"] for item in locked.read()["services"]] == ["stop_intent", "stop_intent"]


def test_closed_session_stays_invalid_when_all_descriptors_are_reused(root):
    with journal.locked_journal(root) as locked:
        begin(locked)
        closure = dict(
            zip(
                locked._check_identity.__code__.co_freevars,
                (cell.cell_contents for cell in locked._check_identity.__closure__),
                strict=True,
            )
        )
        fds = {closure["lock"]}
        for parent, _, child in closure["links"]:
            fds.update((parent, child))
        descriptor_paths = [(fd, os.readlink(f"/proc/self/fd/{fd}")) for fd in sorted(fds)]
    before = (root / "crm-recovery.json").read_bytes()
    reopened = []
    try:
        for old_fd, path in descriptor_paths:
            flags = os.O_RDWR if old_fd == closure["lock"] else os.O_RDONLY | os.O_DIRECTORY
            new_fd = os.open(path, flags)
            reopened.append(new_fd)
            assert new_fd == old_fd
        with pytest.raises(journal.RecoveryJournalError, match="session_closed"):
            transition(locked, "untouched", "stop_intent")
        assert (root / "crm-recovery.json").read_bytes() == before
    finally:
        for fd in reversed(reopened):
            os.close(fd)


def test_context_exit_waits_for_inflight_transaction(root, monkeypatch):
    context = journal.locked_journal(root)
    locked = context.__enter__()
    begin(locked)
    entered = threading.Event()
    release = threading.Event()
    exit_done = threading.Event()
    failures = []
    original = locked._save

    def blocked_save(state):
        entered.set()
        assert release.wait(5)
        original(state)

    def mutate():
        try:
            transition(locked, "untouched", "stop_intent")
        except BaseException as error:  # noqa: BLE001 - relay failures from a worker thread.
            failures.append(error)

    def close():
        try:
            context.__exit__(None, None, None)
        except BaseException as error:  # noqa: BLE001 - relay failures from a worker thread.
            failures.append(error)
        finally:
            exit_done.set()

    monkeypatch.setattr(locked, "_save", blocked_save)
    writer = threading.Thread(target=mutate)
    closer = threading.Thread(target=close)
    writer.start()
    assert entered.wait(5)
    closer.start()
    try:
        assert not exit_done.wait(0.1)
        with pytest.raises(journal.RecoveryJournalError, match="recovery_busy"):
            with journal.locked_journal(root):
                pass
    finally:
        release.set()
        writer.join(5)
        closer.join(5)
    assert not writer.is_alive() and not closer.is_alive() and not failures
    with pytest.raises(journal.RecoveryJournalError, match="session_closed"):
        locked.read()
    with journal.locked_journal(root) as fresh:
        assert fresh.read()["services"][0]["step"] == "stop_intent"


def test_interrupted_invalidation_retains_lock_until_process_exit(root, monkeypatch):
    context = journal.locked_journal(root)
    locked = context.__enter__()
    begin(locked)
    closure = dict(
        zip(
            locked._check_identity.__code__.co_freevars,
            (cell.cell_contents for cell in locked._check_identity.__closure__),
            strict=True,
        )
    )
    descriptors = {closure["lock"]}
    for parent, _, child in closure["links"]:
        descriptors.update((parent, child))
    entered = threading.Event()
    release = threading.Event()
    failures = []
    original_save = locked._save
    original_invalidate = locked._invalidate

    def blocked_save(state):
        entered.set()
        assert release.wait(5)
        original_save(state)

    def mutate():
        try:
            transition(locked, "untouched", "stop_intent")
        except BaseException as error:  # noqa: BLE001 - relay failures from a worker thread.
            failures.append(error)

    def interrupted_invalidate():
        locked._valid = False
        raise KeyboardInterrupt("synthetic interruption while waiting")

    monkeypatch.setattr(locked, "_save", blocked_save)
    monkeypatch.setattr(locked, "_invalidate", interrupted_invalidate)
    writer = threading.Thread(target=mutate)
    writer.start()
    assert entered.wait(5)
    try:
        with pytest.raises(KeyboardInterrupt):
            context.__exit__(None, None, None)
        with pytest.raises(journal.RecoveryJournalError, match="recovery_busy"):
            with journal.locked_journal(root):
                pass
        # Additional interruptions cannot trigger a deferred release either.
        with pytest.raises(KeyboardInterrupt):
            locked._invalidate()
        with pytest.raises(journal.RecoveryJournalError, match="recovery_busy"):
            with journal.locked_journal(root):
                pass
    finally:
        release.set()
        writer.join(5)
        assert not writer.is_alive() and not failures
        # Test-only cleanup after proving the active transaction has finished.
        original_invalidate()
        fcntl.flock(closure["lock"], fcntl.LOCK_UN)
        for fd in sorted(descriptors, reverse=True):
            os.close(fd)
    with pytest.raises(journal.RecoveryJournalError, match="session_closed"):
        locked.read()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_session_cannot_write_or_unlock_parents_flock(root):
    context = journal.locked_journal(root)
    locked = context.__enter__()
    begin(locked)
    mutex_held = threading.Event()
    release_mutex = threading.Event()

    def hold_mutex():
        with locked._mutex:
            mutex_held.set()
            release_mutex.wait(5)

    holder = threading.Thread(target=hold_mutex)
    holder.start()
    assert mutex_held.wait(5)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                transition(locked, "untouched", "stop_intent")
            except journal.RecoveryJournalError as error:
                outcome = str(error)
            else:
                outcome = "unexpected_success"
            # Child teardown must close its own fds without unlocking the
            # parent's shared open file description.
            context.__exit__(None, None, None)
            os.write(write_fd, outcome.encode("ascii"))
            os._exit(0)
        except BaseException:  # noqa: BLE001 - the forked child must exit on any failure.
            os._exit(1)
    os.close(write_fd)
    reaped = False
    try:
        ready, _, _ = select.select([read_fd], [], [], 5)
        assert ready, "child did not finish"
        assert os.read(read_fd, 128) == b"recovery_session_wrong_process"
        _, status = os.waitpid(pid, 0)
        reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
        release_mutex.set()
        holder.join(5)
        assert not holder.is_alive()
        with pytest.raises(journal.RecoveryJournalError, match="recovery_busy"):
            with journal.locked_journal(root):
                pass
        assert locked.read()["services"][0]["step"] == "untouched"
        transition(locked, "untouched", "stop_intent")
    finally:
        release_mutex.set()
        holder.join(5)
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(read_fd)
        context.__exit__(None, None, None)
