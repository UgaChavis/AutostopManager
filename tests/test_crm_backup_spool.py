import multiprocessing
import os
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
