import multiprocessing
import os
import select
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import crm_backup_spool as spool


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "spool"
    path.mkdir(mode=0o700)
    return path


def publish(locked, path, body):
    path.write_bytes(body)
    with path.open("rb") as source:
        return locked.publish(source.fileno())


def read_pending(locked):
    with locked.pending() as (name, fd):
        return name, None if fd is None else os.read(fd, 100)


def test_publish_replace_acknowledge_and_preserve_unrelated(root, tmp_path):
    unrelated = root / "other.txt"
    unrelated.write_text("preserve")
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
        second = publish(locked, tmp_path / "second", b"second")
        assert first != second and read_pending(locked) == (second, b"second")
        assert not (root / first).exists()
        with pytest.raises(spool.SpoolError, match="generation_mismatch"):
            locked.acknowledge(first)
        assert read_pending(locked) == (second, b"second")
        locked.acknowledge(second)
        assert read_pending(locked) == (None, None)
    assert unrelated.read_text() == "preserve"


def test_size_limit_and_failed_publish_preserve_previous(root, tmp_path, monkeypatch):
    with spool.locked_spool(root, max_bytes=6) as locked:
        previous = publish(locked, tmp_path / "first", b"first")
        with pytest.raises(spool.SpoolError, match="source_invalid"):
            publish(locked, tmp_path / "oversized", b"1234567")
        original_replace = os.replace

        def fail_pointer(*args, **kwargs):
            raise OSError("synthetic failure before pointer publication")

        monkeypatch.setattr(spool.os, "replace", fail_pointer)
        with pytest.raises(OSError):
            publish(locked, tmp_path / "second", b"second")
        assert read_pending(locked) == (previous, b"first")
        monkeypatch.setattr(spool.os, "replace", original_replace)
        current = publish(locked, tmp_path / "third", b"third")
        assert read_pending(locked) == (current, b"third")
        assert len(list(root.glob("crm-pending-*.bundle"))) == 1


def test_unknown_pointer_commit_keeps_both_generations(root, tmp_path, monkeypatch):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
        original_fsync = os.fsync
        pointer_replaced = False
        original_replace = os.replace

        def replace(*args, **kwargs):
            nonlocal pointer_replaced
            original_replace(*args, **kwargs)
            pointer_replaced = True

        def fsync(fd):
            if pointer_replaced and stat_is_directory(fd):
                raise OSError("synthetic directory sync failure")
            return original_fsync(fd)

        monkeypatch.setattr(spool.os, "replace", replace)
        monkeypatch.setattr(spool.os, "fsync", fsync)
        with pytest.raises(OSError):
            publish(locked, tmp_path / "second", b"second")
        assert (root / first).read_bytes() == b"first"
        assert len(list(root.glob("crm-pending-*.bundle"))) == 2


def stat_is_directory(fd):
    import stat

    return stat.S_ISDIR(os.fstat(fd).st_mode)


def test_interrupted_ack_requires_recovery_and_preserves_data(root, tmp_path, monkeypatch):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
        original_fsync = os.fsync

        def fail_directory(fd):
            if stat_is_directory(fd):
                raise OSError("synthetic acknowledgement failure")
            return original_fsync(fd)

        monkeypatch.setattr(spool.os, "fsync", fail_directory)
        with pytest.raises(OSError):
            locked.acknowledge(first)
        assert (root / first).read_bytes() == b"first"
        with pytest.raises(spool.SpoolError, match="recovery_required"):
            read_pending(locked)


def test_rejects_unsafe_directory_and_pointer(root, tmp_path):
    root.chmod(0o755)
    with pytest.raises(spool.SpoolError, match="directory_unsafe"):
        with spool.locked_spool(root, max_bytes=100):
            pass
    root.chmod(0o700)
    with spool.locked_spool(root, max_bytes=100):
        pass
    (root / "crm-pending.current").symlink_to(tmp_path / "outside")
    with spool.locked_spool(root, max_bytes=100) as locked:
        with pytest.raises(OSError):
            read_pending(locked)


def _wait_for_lock(path: Path, connection):
    try:
        with spool.locked_spool(path, max_bytes=100):
            connection.send("acquired")
    except spool.SpoolError as error:
        connection.send(str(error))
    connection.close()


def test_transfer_lock_returns_busy_without_waiting(root):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    with spool.locked_spool(root, max_bytes=100):
        process = context.Process(target=_wait_for_lock, args=(root, child))
        process.start()
        assert parent.poll(5) and parent.recv() == "spool_busy"
    process.join(5)
    assert process.exitcode == 0
    with spool.locked_spool(root, max_bytes=100):
        pass


def test_closed_handle_cannot_write_to_reused_descriptor(root, tmp_path):
    other = tmp_path / "other-spool"
    other.mkdir(mode=0o700)
    with spool.locked_spool(root, max_bytes=100) as stale:
        old_fd = stale.directory
    with spool.locked_spool(other, max_bytes=100) as live:
        # A reused descriptor must never make the old object usable again.
        assert stale.directory == old_fd
        with pytest.raises(spool.SpoolError, match="context_closed"):
            publish(stale, tmp_path / "source", b"synthetic")
        assert read_pending(live) == (None, None)


def test_other_thread_cannot_ack_or_publish_with_owner_handle(root, tmp_path):
    failures = []
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")

        def attempt():
            for operation in (
                lambda: locked.acknowledge(first),
                lambda: publish(locked, source, b"synthetic"),
            ):
                try:
                    operation()
                except spool.SpoolError as exc:
                    failures.append(str(exc))

        worker = threading.Thread(target=attempt)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        assert failures == ["spool_wrong_thread", "spool_wrong_thread"]
        assert read_pending(locked) == (first, b"first")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_cannot_use_or_unlock_parent_context(root):
    context = spool.locked_spool(root, max_bytes=100)
    locked = context.__enter__()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            try:
                with locked.pending():
                    pass
            except spool.SpoolError as exc:
                status = str(exc)
            else:
                status = "unexpected_success"
            context.__exit__(None, None, None)
            os.write(write_fd, status.encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 100) == b"spool_wrong_process"
        _, child_status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(child_status) == 0
        with pytest.raises(spool.SpoolError, match="spool_busy"):
            with spool.locked_spool(root, max_bytes=100):
                pass
    finally:
        os.close(read_fd)
        context.__exit__(None, None, None)


def test_interrupted_close_invalidates_handle_before_releasing_lock(root, tmp_path, monkeypatch):
    context = spool.locked_spool(root, max_bytes=100)
    stale = context.__enter__()
    directory = stale.directory
    original_close = os.close

    def interrupt_directory(fd):
        if fd == directory:
            signal.raise_signal(signal.SIGINT)
        return original_close(fd)

    monkeypatch.setattr(spool.os, "close", interrupt_directory)
    try:
        with pytest.raises(KeyboardInterrupt):
            context.__exit__(None, None, None)
    finally:
        monkeypatch.setattr(spool.os, "close", original_close)
    try:
        with pytest.raises(spool.SpoolError, match="context_closed"):
            publish(stale, tmp_path / "source", b"synthetic")
        with pytest.raises(spool.SpoolError, match="spool_busy"):
            with spool.locked_spool(root, max_bytes=100):
                pass
    finally:
        original_close(directory)
        original_close(stale._directory_guard)
        original_close(stale._lock)
        original_close(stale._lock_guard)
    with spool.locked_spool(root, max_bytes=100) as live:
        assert read_pending(live) == (None, None)


def test_unclosed_pending_keeps_lock_until_borrow_ends(root, tmp_path):
    context = spool.locked_spool(root, max_bytes=100)
    locked = context.__enter__()
    first = publish(locked, tmp_path / "source", b"first")
    pending = locked.pending()
    name, fd = pending.__enter__()
    assert name == first and os.read(fd, 100) == b"first"
    with pytest.raises(spool.SpoolError, match="pending_active"):
        context.__exit__(None, None, None)
    with pytest.raises(spool.SpoolError, match="spool_busy"):
        with spool.locked_spool(root, max_bytes=100):
            pass
    pending.__exit__(None, None, None)
    with spool.locked_spool(root, max_bytes=100) as fresh:
        assert read_pending(fresh) == (first, b"first")


def test_replaced_lock_or_directory_invalidates_handle(root):
    with spool.locked_spool(root, max_bytes=100) as locked:
        (root / ".crm-spool.lock").rename(root / ".old-lock")
        (root / ".crm-spool.lock").touch(mode=0o600)
        with pytest.raises(spool.SpoolError, match="descriptor_changed"):
            read_pending(locked)
    (root / ".crm-spool.lock").unlink()
    (root / ".old-lock").rename(root / ".crm-spool.lock")
    with spool.locked_spool(root, max_bytes=100) as locked:
        root.rename(root.with_name("moved"))
        root.mkdir(mode=0o700)
        with pytest.raises(spool.SpoolError, match="descriptor_changed"):
            read_pending(locked)


def test_reopened_same_lock_inode_is_not_mistaken_for_owned_fd(root):
    context = spool.locked_spool(root, max_bytes=100)
    stale = context.__enter__()
    old_fd = stale._lock
    os.close(old_fd)
    unrelated = os.open(root / ".crm-spool.lock", os.O_RDWR)
    assert unrelated == old_fd
    try:
        with pytest.raises(spool.SpoolError, match="descriptor_changed"):
            read_pending(stale)
        with pytest.raises(spool.SpoolError, match="spool_busy"):
            with spool.locked_spool(root, max_bytes=100):
                pass
        with pytest.raises(spool.SpoolError, match="descriptor_changed"):
            context.__exit__(None, None, None)
        os.fstat(unrelated)
    finally:
        os.close(unrelated)
        os.close(stale.directory)
        os.close(stale._directory_guard)
        os.close(stale._lock_guard)


def test_interruption_during_borrow_registration_does_not_leak_ticket(root, tmp_path):
    class InterruptingSet(set):
        def add(self, value):
            super().add(value)
            raise KeyboardInterrupt

    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "source", b"first")
        locked._borrowed = InterruptingSet()
        with pytest.raises(KeyboardInterrupt):
            with locked.pending():
                pass
        assert not locked._borrowed
        locked._borrowed = set()
        assert read_pending(locked) == (first, b"first")


@pytest.mark.parametrize("same_inode", [False, True])
def test_borrowed_fd_reuse_does_not_close_unrelated_file(root, tmp_path, same_inode):
    context = spool.locked_spool(root, max_bytes=100)
    locked = context.__enter__()
    publish(locked, tmp_path / "source", b"first")
    pending = locked.pending()
    generation, borrowed = pending.__enter__()
    guard = pending.gen.gi_frame.f_locals["fd"]
    other = root / generation if same_inode else tmp_path / "other"
    if not same_inode:
        other.write_bytes(b"other")
    os.close(borrowed)
    unrelated = os.open(other, os.O_RDONLY)
    assert unrelated == borrowed
    try:
        with pytest.raises(spool.SpoolError, match="descriptor_changed"):
            pending.__exit__(None, None, None)
        os.fstat(unrelated)
        with pytest.raises(spool.SpoolError, match="recovery_required"):
            context.__exit__(None, None, None)
        with pytest.raises(spool.SpoolError, match="spool_busy"):
            with spool.locked_spool(root, max_bytes=100):
                pass
    finally:
        for fd in (unrelated, guard, locked.directory, locked._directory_guard, locked._lock, locked._lock_guard):
            os.close(fd)


def test_interrupted_borrow_close_retains_spool_lock(root, tmp_path, monkeypatch):
    context = spool.locked_spool(root, max_bytes=100)
    locked = context.__enter__()
    publish(locked, tmp_path / "source", b"first")
    pending = locked.pending()
    _, borrowed = pending.__enter__()
    guard = pending.gen.gi_frame.f_locals["fd"]
    with pytest.raises(spool.SpoolError, match="pending_active"):
        context.__exit__(None, None, None)
    original_close = os.close

    def interrupt_borrow(fd):
        if fd == borrowed:
            signal.raise_signal(signal.SIGINT)
        return original_close(fd)

    monkeypatch.setattr(spool.os, "close", interrupt_borrow)
    try:
        with pytest.raises(KeyboardInterrupt):
            pending.__exit__(None, None, None)
    finally:
        monkeypatch.setattr(spool.os, "close", original_close)
    try:
        os.fstat(borrowed)
        with pytest.raises(spool.SpoolError, match="spool_busy"):
            with spool.locked_spool(root, max_bytes=100):
                pass
    finally:
        for fd in (borrowed, guard, locked.directory, locked._directory_guard, locked._lock, locked._lock_guard):
            original_close(fd)


def test_interrupt_after_borrow_discard_completes_deferred_release(root, tmp_path):
    class InterruptingDiscard(set):
        def discard(self, value):
            super().discard(value)
            raise KeyboardInterrupt

    context = spool.locked_spool(root, max_bytes=100)
    locked = context.__enter__()
    first = publish(locked, tmp_path / "source", b"first")
    locked._borrowed = InterruptingDiscard()
    pending = locked.pending()
    pending.__enter__()
    with pytest.raises(spool.SpoolError, match="pending_active"):
        context.__exit__(None, None, None)
    with pytest.raises(KeyboardInterrupt):
        pending.__exit__(None, None, None)
    with spool.locked_spool(root, max_bytes=100) as fresh:
        assert read_pending(fresh) == (first, b"first")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_child_inherits_borrow_lock_until_it_exits(root, tmp_path):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "source", b"first")
        pending = locked.pending()
        pending.__enter__()
        child_read, parent_write = os.pipe()
        parent_read, child_write = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(parent_write)
                os.close(parent_read)
                try:
                    pending.__exit__(None, None, None)
                except spool.SpoolError as exc:
                    os.write(child_write, str(exc).encode())
                os.read(child_read, 1)
            finally:
                os._exit(0)
        os.close(child_read)
        os.close(child_write)
        try:
            assert select.select([parent_read], [], [], 3)[0]
            assert os.read(parent_read, 100) == b"spool_wrong_process"
            pending.__exit__(None, None, None)
            with pytest.raises(BlockingIOError):
                read_pending(locked)
        finally:
            os.write(parent_write, b"x")
            os.close(parent_write)
            os.close(parent_read)
            _, child_status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(child_status) == 0
        assert read_pending(locked) == (first, b"first")


def test_first_publication_interrupted_before_pointer_requires_recovery(root, tmp_path, monkeypatch):
    def fail_pointer(*args, **kwargs):
        raise OSError("synthetic interruption")

    with spool.locked_spool(root, max_bytes=100) as locked:
        monkeypatch.setattr(spool.os, "replace", fail_pointer)
        with pytest.raises(OSError):
            publish(locked, tmp_path / "first", b"first")
        assert len(list(root.glob("crm-pending-*.bundle"))) == 1
        with pytest.raises(spool.SpoolError, match="recovery_required"):
            read_pending(locked)
        with pytest.raises(spool.SpoolError, match="recovery_required"):
            publish(locked, tmp_path / "second", b"second")


def test_existing_generation_never_replaced_on_collision(root, tmp_path, monkeypatch):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
        nonce = first.split("-")[2]
        original_uuid = spool.uuid4
        calls = 0

        def collision_once():
            nonlocal calls
            calls += 1
            return SimpleNamespace(hex=nonce) if calls == 1 else original_uuid()

        monkeypatch.setattr(spool, "uuid4", collision_once)
        with pytest.raises(FileExistsError):
            publish(locked, tmp_path / "second", b"second")
        assert read_pending(locked) == (first, b"first")


def test_interruption_between_link_and_unlink_keeps_previous(root, tmp_path, monkeypatch):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
        original_unlink = spool.os.unlink

        def fail_temporary(name, *args, **kwargs):
            if name.startswith(".crm-spool-"):
                raise OSError("synthetic interruption")
            return original_unlink(name, *args, **kwargs)

        monkeypatch.setattr(spool.os, "unlink", fail_temporary)
        with pytest.raises(OSError):
            publish(locked, tmp_path / "second", b"second")
        assert read_pending(locked) == (first, b"first")
        monkeypatch.setattr(spool.os, "unlink", original_unlink)
        with pytest.raises(spool.SpoolError, match="spool_file_unsafe"):
            publish(locked, tmp_path / "third", b"third")
        assert read_pending(locked) == (first, b"first")


def test_unknown_reserved_file_never_deleted(root, tmp_path):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
        foreign = root / ("crm-pending-" + "f" * 32 + "-" + "f" * 64 + ".bundle")
        foreign.write_bytes(b"other")
        foreign.chmod(0o600)
        with pytest.raises(spool.SpoolError, match="unknown_file"):
            publish(locked, tmp_path / "second", b"second")
        assert foreign.read_bytes() == b"other"
        assert read_pending(locked) == (first, b"first")


@pytest.mark.parametrize("failure", ["missing", "truncated", "replaced"])
def test_missing_or_corrupt_key_preserves_pending(root, tmp_path, failure):
    with spool.locked_spool(root, max_bytes=100) as locked:
        first = publish(locked, tmp_path / "first", b"first")
    key = root / ".crm-spool.key"
    if failure == "missing":
        key.unlink()
    else:
        key.write_bytes(b"x" if failure == "truncated" else b"x" * 32)
    with pytest.raises(spool.SpoolError):
        with spool.locked_spool(root, max_bytes=100) as locked:
            publish(locked, tmp_path / "second", b"second")
    assert (root / first).read_bytes() == b"first"


def _fifo_probe(path, connection):
    try:
        with spool.locked_spool(path, max_bytes=100) as locked:
            read_pending(locked)
    except (OSError, spool.SpoolError):
        connection.send("rejected")
    finally:
        connection.close()


def test_fifo_pointer_is_rejected_without_blocking(root):
    with spool.locked_spool(root, max_bytes=100):
        pass
    os.mkfifo(root / "crm-pending.current", 0o600)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_fifo_probe, args=(root, child))
    process.start()
    if not parent.poll(5):
        process.terminate()
        process.join(5)
        pytest.fail("FIFO open blocked")
    assert parent.recv() == "rejected"
    process.join(5)
    assert process.exitcode == 0
