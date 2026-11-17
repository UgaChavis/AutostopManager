"""Derived role views stay current across concurrent writers and receipt replay."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest

from autostop_manager import role_coordination
from autostop_manager.role_coordination import ROLES, RoleStore


def _checkpoint(store, text, key, revision):
    return store.action("checkpoint", "M1", {"text": text}, idempotency_key=key, expected_revision=revision)


def test_projection_snapshot_is_serialized_with_concurrent_checkpoint(tmp_path, monkeypatch):
    first = RoleStore(tmp_path / "registry.sqlite3")
    second = RoleStore(first.path)
    captured, release, second_started, second_finished = Event(), Event(), Event(), Event()
    results, failures = {}, []
    replace = role_coordination.os.replace

    def delayed_replace(source, target):
        if current_thread() is first_thread and Path(target) == first.views_root / "M1" / "context.md":
            captured.set()
            assert release.wait(5), "projection release timed out"
        return replace(source, target)

    def checkpoint(store, text, key, revision):
        try:
            if store is second:
                second_started.set()
            results[key] = _checkpoint(store, text, key, revision)
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures to the test thread.
            failures.append(exc)
        finally:
            if store is second:
                second_finished.set()

    monkeypatch.setattr(role_coordination.os, "replace", delayed_replace)
    first_thread = Thread(target=checkpoint, args=(first, "older context", "older", 0))
    second_thread = Thread(target=checkpoint, args=(second, "newer context", "newer", 1))
    first_thread.start()
    try:
        assert captured.wait(5), "first projection did not reach replace"
        second_thread.start()
        assert second_started.wait(5)
        # The first snapshot's pending replace must exclude a newer CAS commit.
        finished_before_release = second_finished.wait(0.25)
    finally:
        release.set()
        first_thread.join(5)
        if second_thread.ident is not None:
            second_thread.join(5)

    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert not failures, failures
    assert not finished_before_release
    assert results["older"]["role"]["revision"] == 1
    assert results["newer"]["role"]["revision"] == 2
    state = second.role_state("M1")
    assert state["revision"] == 2 and state["context"] == "newer context"
    assert (second.views_root / "M1" / "context.md").read_text() == "# M1 — рабочий контекст\n\nnewer context\n"


@pytest.mark.parametrize("newer_checkpoint", [False, True])
def test_idempotent_retry_rebuilds_views_without_reapplying_command(tmp_path, monkeypatch, newer_checkpoint):
    first = RoleStore(tmp_path / "registry.sqlite3")
    replace = role_coordination.os.replace
    failed = False

    def fail_once(source, target):
        nonlocal failed
        if not failed and Path(target) == first.views_root / "M1" / "notes.md":
            failed = True
            raise OSError("synthetic projection failure")
        return replace(source, target)

    monkeypatch.setattr(role_coordination.os, "replace", fail_once)
    with pytest.raises(OSError, match="synthetic projection failure"):
        _checkpoint(first, "persisted context", "retry", 0)
    assert first.role_state("M1")["revision"] == 1
    assert (first.views_root / "M1" / "context.md").exists()
    assert not (first.views_root / "M1" / "notes.md").exists()
    with first.connection() as db:
        original = json.loads(db.execute("SELECT response FROM role_commands WHERE key='retry'").fetchone()[0])

    recovered = RoleStore(first.path)
    expected_context, expected_revision = "persisted context", 1
    if newer_checkpoint:
        _checkpoint(recovered, "newer context", "newer", 1)
        expected_context, expected_revision = "newer context", 2
        # A replay must derive the latest projection rather than its old receipt.
        (recovered.views_root / "M1" / "context.md").unlink()
        (recovered.views_root / "M1" / "notes.md").unlink()

    response = _checkpoint(recovered, "persisted context", "retry", 0)
    assert response == original
    assert _checkpoint(recovered, "persisted context", "retry", 0) == original
    state = recovered.role_state("M1")
    assert state["revision"] == expected_revision and state["context"] == expected_context
    with recovered.connection() as db:
        assert db.execute("SELECT count(*) FROM role_commands").fetchone()[0] == expected_revision
    assert (recovered.views_root / "M1" / "context.md").read_text() == (
        f"# M1 — рабочий контекст\n\n{expected_context}\n"
    )
    for role in ROLES:
        assert (recovered.views_root / role / "context.md").is_file()
        assert (recovered.views_root / role / "notes.md").is_file()
    assert not list(recovered.views_root.rglob(".role-*"))
