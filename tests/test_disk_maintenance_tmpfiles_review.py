"""Synthetic installer regressions for the mandatory Telegram control-lock prerequisite.

Includes legacy lock-mode normalization and strict confirmation before enabling timers.
Linux/root fixtures use private paths and a private Git source. The command fake
models only the selected canonical f-rule at a private runtime leaf; it does not
prove native systemd-tmpfiles or service namespace behavior. Product publication,
rollback, metadata, fsync, and flock remain real. No service/helper is executed.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.disk_maintenance import install as installer
from scripts.disk_maintenance.core import DEFAULT_POLICY
from scripts.disk_maintenance.util import MaintenanceError

ASSET = "deploy/tmpfiles.d/autostop-disk-maintenance-locks.conf"
RULE = b"f /run/autostop-work-telegram-control.lock 0600 root root -\n"
COMMENTED_RULE = b"\n# Synthetic fixture ownership marker; preserve these bytes.\n \t\n" + RULE + b"\n"
RUNTIME_NAME = "/run/autostop-work-telegram-control.lock"
REPOSITORY = Path(installer.__file__).resolve().parents[2]


def _git(root, *arguments):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()


def _write_private(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _leaf(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    kind = "file" if stat.S_ISREG(info.st_mode) else "link" if stat.S_ISLNK(info.st_mode) else "other"
    return {
        "kind": kind,
        "identity": (info.st_dev, info.st_ino),
        "mode": stat.S_IMODE(info.st_mode),
        "owner": (info.st_uid, info.st_gid),
        "nlink": info.st_nlink,
        "data": path.read_bytes() if kind == "file" else os.readlink(path) if kind == "link" else None,
    }


def _safe_rule(row):
    return bool(
        row
        and row["kind"] == "file"
        and row["owner"] == (0, 0)
        and not row["mode"] & 0o022
        and row["nlink"] == 1
        and [line for line in row["data"].splitlines() if line.strip() and not line.lstrip().startswith(b"#")]
        == [RULE.rstrip(b"\n")]
    )


def _safe_lock(row):
    return bool(row and row["kind"] == "file" and row["owner"] == (0, 0) and row["mode"] == 0o600 and row["nlink"] == 1)


def _hold(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _assert_still_locked(path, holder):
    original = os.fstat(holder)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        actual = os.fstat(descriptor)
        assert (actual.st_dev, actual.st_ino) == (original.st_dev, original.st_ino)
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


@pytest.fixture
def source(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("native installer ownership fixtures require root")
    root = tmp_path / "source"
    root.mkdir()
    names = {
        *installer.PAYLOAD,
        *installer.INSTALL_SOURCE,
        *("deploy/systemd/" + name for name in installer.UNITS),
        ASSET,
    }
    for name in sorted(names):
        # The baseline lacks ASSET. Supplying it here tests installer behavior,
        # rather than failing source construction before the public install call.
        data = RULE if name == ASSET else (REPOSITORY / name).read_bytes()
        _write_private(root / name, data, 0o644)
    for arguments in (
        ("init", "-q"),
        ("config", "user.email", "fixture@example.invalid"),
        ("config", "user.name", "Fixture"),
        ("add", "."),
        ("commit", "-qm", "synthetic selected source"),
    ):
        _git(root, *arguments)
    return root, _git(root, "rev-parse", "HEAD")


class CommandBoundary:
    def __init__(self, source_root, rule, lock):
        self.source_root = source_root
        self.rule = rule
        self.lock = lock
        self.actual = installer._command
        self.calls = []
        self.events = []
        self.states = {name: {"enabled": "not-found", "active": "inactive"} for name in installer.TIMERS}
        self.tmpfiles_mode = "normal"
        self.late_failure = None
        self.late_fired = False
        self.foreign_rule = None
        self.published_rule = None
        self.lock_at_failure = None
        self.holder = None

    def __call__(self, argv, *, allow_failure=False):
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        executable = Path(argv[0]).name
        if executable == "git":
            assert argv[:3] == ["git", "-C", str(self.source_root)]
            assert argv[3] in {"rev-parse", "status", "ls-files"}
            return self.actual(argv, allow_failure=allow_failure)
        if executable == "systemd-tmpfiles":
            return self._tmpfiles(argv)
        if argv == [str(installer.WRAPPER), "--policy", str(installer.POLICY), "--help"]:
            return "synthetic help"
        if executable == "systemd-analyze":
            assert argv == [
                "systemd-analyze",
                "verify",
                *(str(installer.SYSTEMD / name) for name in installer.UNITS[:4]),
            ]
            return ""
        assert executable == "systemctl", f"unapproved command boundary: {argv}"
        return self._systemctl(argv)

    def _tmpfiles(self, argv):
        assert argv[1:] in (["--create", str(self.rule)], ["--create", "--", str(self.rule)])
        self.events.append(("tmpfiles", None))
        if self.tmpfiles_mode == "failure":
            raise MaintenanceError("synthetic_tmpfiles_failure")
        if self.tmpfiles_mode == "zero-without-leaf":
            return ""
        # Exactly one canonical active rule maps to this private leaf; whole-line
        # comments and blank lines do not authorize any additional operation.
        if not _safe_rule(_leaf(self.rule)):
            raise MaintenanceError("synthetic_tmpfiles_invalid_rule")
        existing = _leaf(self.lock)
        if existing is not None and existing["kind"] != "file":
            raise MaintenanceError("synthetic_tmpfiles_unsafe_leaf")
        descriptor = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            # Native f normalizes an existing regular inode in place. Whether
            # that inode is trusted for adoption is checked by the installer.
            os.fchown(descriptor, 0, 0)
            if self.tmpfiles_mode != "zero-without-chmod":
                os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.events.append(("bootstrap-ready", _leaf(self.lock)))
        return ""

    def _systemctl(self, argv):
        action = argv[1]
        if action == "daemon-reload":
            assert len(argv) == 2
            for name, state in self.states.items():
                if not (installer.SYSTEMD / name).exists():
                    state["enabled"] = "not-found"
                elif state["enabled"] == "not-found":
                    state["enabled"] = "disabled"
            return ""
        name = argv[-1]
        assert name in self.states
        state = self.states[name]
        if action in {"is-enabled", "is-active"}:
            return state["enabled" if action == "is-enabled" else "active"]
        assert action in {"enable", "disable", "start", "stop"}
        if action == "enable":
            # Observe prerequisites; do not manufacture a baseline product error.
            self.events.append(("enable", (_leaf(self.rule), _leaf(self.lock))))
            state["enabled"] = "enabled-runtime" if "--runtime" in argv else "enabled"
            if "--now" in argv:
                state["active"] = "active"
            self._fail_late(name)
        elif action == "disable":
            state["enabled"] = "disabled" if (installer.SYSTEMD / name).exists() else "not-found"
        else:
            state["active"] = "active" if action == "start" else "inactive"
        return ""

    def _fail_late(self, name):
        if self.late_failure is None or self.late_fired or name != installer.TIMERS[1]:
            return
        self.late_fired = True
        self.published_rule = _leaf(self.rule)
        if _safe_lock(_leaf(self.lock)):
            self.holder = _hold(self.lock)
            self.lock_at_failure = _leaf(self.lock)
        if self.published_rule is not None and self.late_failure == "foreign-inode":
            replacement = self.rule.with_name("foreign-replacement.conf")
            _write_private(replacement, RULE, 0o644)
            os.replace(replacement, self.rule)
            self.foreign_rule = _leaf(self.rule)
        elif self.published_rule is not None and self.late_failure == "foreign-bytes":
            self.rule.write_bytes(b"# synthetic foreign rule contents\n")
            self.foreign_rule = _leaf(self.rule)
        raise MaintenanceError("synthetic_late_activation_failure")


@pytest.fixture
def target(tmp_path, monkeypatch, source):
    root = tmp_path / "system"
    root.mkdir()
    for name, relative in (
        ("BASE", "libexec"),
        ("POLICY", "etc/policy.json"),
        ("STATE", "state"),
        ("WRAPPER", "sbin/helper"),
        ("SYSTEMD", "systemd"),
        ("TMPFILES", "etc/tmpfiles.d/autostop-disk-maintenance-locks.conf"),
        ("CONTROL_LOCK", "run/autostop-work-telegram-control.lock"),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # New globals are fixture adapters; the old installer safely ignores them.
        monkeypatch.setattr(installer, name, path, raising=False)
    installer.SYSTEMD.mkdir()
    policy = tmp_path / "approved" / "policy.json"
    approved = deepcopy(DEFAULT_POLICY)
    approved["state_root"] = str(installer.STATE)
    approved["filesystem_root"] = str(root)
    approved["locks"] = {
        name: str(installer.CONTROL_LOCK if name == "tg" else root / "run" / f"{name}.lock")
        for name in approved["locks"]
    }
    approved["inventory"]["roots"] = {name: str(root / "inventory" / name) for name in approved["inventory"]["roots"]}
    approved["inventory"]["store_deploy_marker"] = str(root / "synthetic-marker.env")
    # CI identity literals are required by the existing policy validator. No CI
    # command is allowed through CommandBoundary; no runner path is accessed.
    _write_private(policy, json.dumps(approved).encode())
    policy.parent.chmod(0o700)
    receipt = tmp_path / "receipts" / "install.json"
    receipt.parent.mkdir(mode=0o700)
    boundary = CommandBoundary(source[0], installer.TMPFILES, installer.CONTROL_LOCK)
    real_run = subprocess.run

    def only_git(argv, **kwargs):
        assert argv[0] == "git", "external commands must use the controlled boundary"
        assert str(source[0]) in argv
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", only_git)
    monkeypatch.setattr(installer, "_command", boundary)
    yield SimpleNamespace(
        policy=policy, receipt=receipt, command=boundary, rule=installer.TMPFILES, lock=installer.CONTROL_LOCK
    )
    if boundary.holder is not None:
        os.close(boundary.holder)


def _install(source, target, receipt=None):
    return installer.install(source[0], target.policy, source[1], receipt or target.receipt)


def _no_enable(target):
    assert not any(event == "enable" for event, _ in target.command.events)


def _assert_bootstrap_before_enable(target, *, initially_missing=False):
    events = target.command.events
    first_enable = next(index for index, (event, _) in enumerate(events) if event == "enable")
    if initially_missing:
        assert any(event == "tmpfiles" for event, _ in events[:first_enable])
        assert any(event == "bootstrap-ready" for event, _ in events[:first_enable])
    rule, lock = events[first_enable][1]
    assert _safe_rule(rule) and _safe_lock(lock)


def test_repository_tracks_the_exact_nontruncating_rule():
    asset = REPOSITORY / ASSET
    assert asset.is_file(), "the selected source must include the boot prerequisite"
    assert asset.read_bytes() == RULE
    assert _git(REPOSITORY, "ls-files", "--", ASSET) == ASSET


@pytest.mark.parametrize("change", ["dirty", "untracked"])
def test_selected_rule_cannot_escape_source_verification(source, target, change):
    root, revision = source
    if change == "dirty":
        (root / ASSET).write_bytes(RULE + b"# uncommitted change\n")
    else:
        _git(root, "rm", "--cached", "--", ASSET)
        _git(root, "commit", "-qm", "synthetic source without tracked prerequisite")
        revision = _git(root, "rev-parse", "HEAD")
    with pytest.raises(MaintenanceError):
        _install((root, revision), target)
    _no_enable(target)
    assert not target.rule.exists() and not target.lock.exists()


@pytest.mark.parametrize("rule_state", ["absent", "plain", "commented"])
@pytest.mark.parametrize("lock_exists", [False, True])
def test_bootstrap_and_reinstall_preserve_adopted_rule_and_held_lock(source, target, rule_state, lock_exists):
    if rule_state != "absent":
        _write_private(target.rule, RULE if rule_state == "plain" else COMMENTED_RULE, 0o644)
    if lock_exists:
        _write_private(target.lock, b"synthetic existing lock bytes\n")
    original_rule, original_lock = _leaf(target.rule), _leaf(target.lock)
    holder = _hold(target.lock) if lock_exists else None
    try:
        assert _install(source, target)["status"] == "installed"
        _assert_bootstrap_before_enable(target, initially_missing=not lock_exists)
        assert any(call[3:4] == ["status"] and ASSET in call for call in target.command.calls)
        assert any("ls-files" in call and call[-1] == ASSET for call in target.command.calls)
        assert _safe_rule(_leaf(target.rule)) and _safe_lock(_leaf(target.lock))
        if original_rule is not None:
            assert _leaf(target.rule) == original_rule
        if original_lock is not None:
            assert _leaf(target.lock) == original_lock
        if holder is None:
            target.lock.write_bytes(b"synthetic bytes after first bootstrap\n")
            holder = _hold(target.lock)
        _assert_still_locked(target.lock, holder)
        adopted_rule, adopted_lock = _leaf(target.rule), _leaf(target.lock)
        target.command.events.clear()
        assert _install(source, target, target.receipt.with_name("reinstall.json"))["status"] == "installed"
        _assert_bootstrap_before_enable(target)
        assert _leaf(target.rule) == adopted_rule
        assert _leaf(target.lock) == adopted_lock
        _assert_still_locked(target.lock, holder)
        service = (installer.SYSTEMD / "autostop-disk-maintenance.service").read_bytes()
        assert service == (source[0] / "deploy/systemd/autostop-disk-maintenance.service").read_bytes()
        write_paths = next(
            line.split("=", 1)[1].split()
            for line in service.decode().splitlines()
            if line.startswith("ReadWritePaths=")
        )
        assert RUNTIME_NAME in write_paths and "-" + RUNTIME_NAME not in write_paths
        assert not any(call[0] == "systemctl" and "restart" in call for call in target.command.calls)
    finally:
        if holder is not None:
            os.close(holder)


def test_legacy_0644_lock_is_normalized_without_changing_held_inode_or_bytes(source, target):
    _write_private(target.lock, b"synthetic legacy lock bytes\n", 0o644)
    before = _leaf(target.lock)
    holder = _hold(target.lock)
    try:
        assert _install(source, target)["status"] == "installed"
        _assert_bootstrap_before_enable(target)
        assert _leaf(target.lock) == {**before, "mode": 0o600}
        _assert_still_locked(target.lock, holder)
        events = [name for name, _ in target.command.events]
        assert events.index("tmpfiles") < events.index("enable")
    finally:
        os.close(holder)


def test_success_without_legacy_lock_mode_normalization_never_enables_timers(source, target):
    _write_private(target.lock, b"synthetic unnormalized legacy lock bytes\n", 0o644)
    target.command.tmpfiles_mode = "zero-without-chmod"
    before = _leaf(target.lock)
    holder = _hold(target.lock)
    try:
        with pytest.raises(MaintenanceError, match="install_failed_rolled_back"):
            _install(source, target)
        assert any(event == "tmpfiles" for event, _ in target.command.events)
        _no_enable(target)
        assert _leaf(target.lock) == before
        _assert_still_locked(target.lock, holder)
        assert json.loads(target.receipt.read_text())["status"] == "rolled_back"
    finally:
        os.close(holder)


@pytest.mark.parametrize(
    "kind",
    [
        "rule-conflict",
        "rule-extra-active",
        "rule-symlink",
        "rule-writable",
        "lock-symlink",
        "lock-directory",
        "lock-hardlink",
        "lock-owner",
        "lock-group-writable",
        "lock-world-writable",
        "lock-other-safe-mode",
    ],
)
def test_conflicting_or_unsafe_existing_objects_are_not_adopted(source, target, kind):
    sentinel = target.rule.parent / "private-sentinel"
    _write_private(sentinel, b"preserve synthetic sentinel\n")
    if kind == "rule-conflict":
        _write_private(target.rule, b"# unrelated existing owner\n", 0o644)
    elif kind == "rule-extra-active":
        _write_private(target.rule, RULE + RULE, 0o644)
    elif kind == "rule-symlink":
        target.rule.symlink_to(sentinel)
    elif kind == "rule-writable":
        _write_private(target.rule, RULE, 0o666)
    elif kind == "lock-symlink":
        target.lock.symlink_to(sentinel)
    elif kind == "lock-directory":
        target.lock.mkdir()
    elif kind == "lock-hardlink":
        os.link(sentinel, target.lock)
    elif kind in {"lock-group-writable", "lock-world-writable", "lock-other-safe-mode"}:
        modes = {"lock-group-writable": 0o660, "lock-world-writable": 0o666, "lock-other-safe-mode": 0o640}
        _write_private(target.lock, b"synthetic unsupported lock mode\n", modes[kind])
    else:
        _write_private(target.lock, b"synthetic foreign owned lock\n")
        os.chown(target.lock, 4242, 0)
    before = [_leaf(path) for path in (target.rule, target.lock, sentinel)]
    with pytest.raises(MaintenanceError):
        _install(source, target)
    assert [_leaf(path) for path in (target.rule, target.lock, sentinel)] == before
    _no_enable(target)
    assert not any(event == "tmpfiles" for event, _ in target.command.events)


@pytest.mark.parametrize("mode", ["failure", "zero-without-leaf"])
def test_tmpfiles_failure_or_unconfirmed_creation_never_enables_timers(source, target, mode):
    target.command.tmpfiles_mode = mode
    with pytest.raises(MaintenanceError):
        _install(source, target)
    assert any(event == "tmpfiles" for event, _ in target.command.events)
    _no_enable(target)
    assert not target.lock.exists()
    if target.receipt.exists():
        assert json.loads(target.receipt.read_text())["status"] != "installed"


@pytest.mark.parametrize("mode", ["created", "adopted", "foreign-inode", "foreign-bytes"])
def test_late_failure_uses_owned_rule_rollback_and_never_removes_runtime_lock(source, target, mode):
    if mode == "adopted":
        _write_private(target.rule, RULE, 0o644)
    original = _leaf(target.rule)
    target.command.late_failure = mode
    with pytest.raises(MaintenanceError) as caught:
        _install(source, target)
    _assert_bootstrap_before_enable(target, initially_missing=True)
    assert target.command.late_fired
    assert target.command.holder is not None
    assert _leaf(target.lock) == target.command.lock_at_failure
    _assert_still_locked(target.lock, target.command.holder)
    receipt = json.loads(target.receipt.read_text())
    if mode.startswith("foreign-"):
        assert target.command.foreign_rule is not None
        assert _leaf(target.rule) == target.command.foreign_rule
        assert caught.value.code == "install_rollback_incomplete"
        assert receipt["status"] == "rollback_required" and receipt["rollback_errors"]
    else:
        assert _leaf(target.rule) == original
        assert caught.value.code == "install_failed_rolled_back"
        assert receipt["status"] == "rolled_back" and receipt["rollback_errors"] == []
    assert all(state == {"enabled": "not-found", "active": "inactive"} for state in target.command.states.values())


class ForeignRuleAfterAbsence:
    """One OS-read interleaving, with actual kernel observation and publication."""

    def __init__(self, path, data):
        self.path = path
        self.data = data
        self.real_open = os.open
        self.real_stat = os.stat
        self.real_lstat = os.lstat
        self.fired = False
        self.command_depth = 0
        self.boundary = None
        self.foreign = None

    def matches(self, path):
        return isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == str(self.path)

    def publish(self, boundary):
        # The saved real open avoids recursion. O_EXCL never overwrites another
        # fixture writer; no product publication call or result is intercepted.
        self.fired = True
        self.boundary = boundary
        descriptor = self.real_open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            os.fchmod(descriptor, 0o644)
            remaining = memoryview(self.data)
            while remaining:
                written = os.write(descriptor, remaining)
                assert written > 0
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.foreign = _leaf(self.path)

    def observe_stat(self, original, boundary, path, *args, **kwargs):
        try:
            return original(path, *args, **kwargs)
        except FileNotFoundError:
            if not self.fired and not self.command_depth and self.matches(path):
                self.publish(boundary)
            raise

    def stat(self, path, *args, **kwargs):
        return self.observe_stat(self.real_stat, "stat", path, *args, **kwargs)

    def lstat(self, path, *args, **kwargs):
        return self.observe_stat(self.real_lstat, "lstat", path, *args, **kwargs)

    def open(self, path, flags, *args, **kwargs):
        try:
            return self.real_open(path, flags, *args, **kwargs)
        except FileNotFoundError:
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            if not self.fired and not self.command_depth and not writing and self.matches(path):
                self.publish("read-only open")
            raise

    def attach(self, monkeypatch):
        command = installer._command

        def controlled_command(*args, **kwargs):
            # Fake command observations must not masquerade as product reads.
            self.command_depth += 1
            try:
                return command(*args, **kwargs)
            finally:
                self.command_depth -= 1

        monkeypatch.setattr(installer, "_command", controlled_command)
        monkeypatch.setattr(os, "stat", self.stat)
        monkeypatch.setattr(os, "lstat", self.lstat)
        monkeypatch.setattr(os, "open", self.open)


def test_foreign_rule_appearing_after_missing_observation_is_preserved(source, target, monkeypatch):
    assert _leaf(target.rule) is None
    assert _leaf(target.lock) is None
    # This active rule is deliberately different, and names only a private leaf.
    foreign_data = f"f {target.lock.with_name('synthetic-foreign.lock')} 0600 root root -\n".encode()
    race = ForeignRuleAfterAbsence(target.rule, foreign_data)
    race.attach(monkeypatch)
    failure = None
    try:
        _install(source, target)
    except MaintenanceError as exc:
        failure = exc
    assert race.fired, (
        "OS observation hook not reached: this result does not prove an overwrite race; "
        "the baseline ignores TMPFILES, while another implementation may use a different read boundary"
    )
    assert race.boundary in {"stat", "lstat", "read-only open"}
    assert race.foreign is not None and race.foreign["data"] == foreign_data
    assert _leaf(target.rule) == race.foreign, "foreign inode, bytes and trusted metadata must survive"
    assert failure is not None, "a conflicting foreign rule must fail closed"
    _no_enable(target)
    assert not any(event == "tmpfiles" for event, _ in target.command.events)
    assert _leaf(target.lock) is None
    if target.receipt.exists():
        assert json.loads(target.receipt.read_text())["status"] != "installed"


def test_committed_noncanonical_source_rule_is_rejected_before_bootstrap(source, target):
    root, _old_revision = source
    asset = root / ASSET
    unapproved = f"f {target.lock.with_name('unapproved-source.lock')} 0600 root root -\n".encode()
    asset.write_bytes(unapproved)
    _git(root, "add", "--", ASSET)
    _git(root, "commit", "-qm", "synthetic committed noncanonical prerequisite")
    revision = _git(root, "rev-parse", "HEAD")
    assert _git(root, "ls-files", "--", ASSET) == ASSET
    assert _git(root, "status", "--porcelain") == ""
    before = _leaf(asset)
    with pytest.raises(MaintenanceError):
        _install((root, revision), target)
    assert _leaf(asset) == before
    assert _leaf(target.rule) is None and _leaf(target.lock) is None
    _no_enable(target)
    assert not any(event == "tmpfiles" for event, _ in target.command.events)
    if target.receipt.exists():
        assert json.loads(target.receipt.read_text())["status"] != "installed"
