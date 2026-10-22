import importlib.util
import os
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "technical_retention", Path(__file__).parents[1] / "scripts/technical-retention.py"
)
retention = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = retention
SPEC.loader.exec_module(retention)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/var/log/syslog.3.gz", True),
        ("/var/log/nginx/access.log.1", True),
        ("/var/log/sysstat/sa20", True),
        ("/var/log/autostop-server-maintenance/maintenance-20260718T060939Z.log", True),
        ("/var/log/auth.log", False),
        ("/var/log/nginx/access.log", False),
        ("/opt/autostopcrm/data/audit.log.3", False),
        ("/var/lib/docker/containers/a/a-json.log.1", False),
        ("/var/log/customer.log.1", False),
        ("/var/log/journal/system.journal", False),
        ("/var/log/btmp.1", False),
        ("/var/log/wtmp.1", False),
    ],
)
def test_only_closed_technical_names(path, expected):
    assert retention.is_rotated_log(Path(path)) is expected


def entry(tmp_path, name, age=1, allocated=100):
    p = tmp_path / name
    p.write_text("synthetic")
    item = retention.Entry.read(p)
    return retention.Entry(p, item.device, item.inode, item.size, allocated, age, age, age, 1)


def test_age_and_budget_preserve_open_files(tmp_path):
    old, recent, opened = [entry(tmp_path, name, age) for name, age in [("old", 1), ("recent", 99), ("open", 0)]]
    selected, remaining = retention.select_cache([recent, opened, old], 5, 150, {(opened.device, opened.inode)})
    assert selected == [old, recent]
    assert remaining == 100


def test_recent_use_survives_age_and_below_budget(tmp_path):
    old = entry(tmp_path, "download")
    touched = retention.Entry(old.path, old.device, old.inode, old.size, old.allocated, 1, 100, 1, 1)
    assert retention.select_cache([touched], 50, 1000, set()) == ([], 100)


def test_symlink_root_and_children_are_excluded(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    actual.joinpath("data").write_text("synthetic")
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    assert retention.regular_files(link) == []
    actual.joinpath("external").symlink_to(actual / "data")
    assert len(retention.regular_files(actual)) == 1


def test_exact_delete_rejects_changed_open_busy_and_hardlinked(tmp_path, monkeypatch):
    p = tmp_path / "download"
    p.write_text("synthetic")
    item = retention.Entry.read(p)
    monkeypatch.setattr(retention, "usage", lambda: ({(item.device, item.inode)}, False))
    assert not retention.remove_exact(item)
    monkeypatch.setattr(retention, "usage", lambda: (set(), True))
    assert not retention.remove_exact(item)
    monkeypatch.setattr(retention, "usage", lambda: (set(), False))
    p.write_text("changed")
    assert not retention.remove_exact(item)
    os.link(p, tmp_path / "linked")
    assert not retention.remove_exact(retention.Entry.read(p))


def test_exact_delete_only_selected_file(tmp_path, monkeypatch):
    p = tmp_path / "old-cache"
    p.write_text("synthetic")
    kept = tmp_path / "tool"
    kept.write_text("preserve")
    monkeypatch.setattr(retention, "usage", lambda: (set(), False))
    assert retention.remove_exact(retention.Entry.read(p))
    assert not p.exists() and kept.read_text() == "preserve"


def test_busy_process_inventory_and_permission_fail_closed(tmp_path):
    proc = tmp_path / "99999999"
    proc.mkdir()
    proc.joinpath("cmdline").write_bytes(b"/usr/bin/python3\x00-m\x00pip\x00install\x00example\x00")
    proc.joinpath("fd").mkdir()
    proc.joinpath("maps").write_text("")
    assert retention.usage(tmp_path)[1]


def test_busy_gate_never_invokes_docker_or_unlinks(monkeypatch):
    monkeypatch.setattr(retention, "usage", lambda: (set(), True))
    monkeypatch.setattr(retention, "regular_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(retention.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected Docker call"))
    result = retention.retain(3, 1024, True, True)
    assert result["docker_status"] == "skipped_busy" and result["removed_bytes"] == 0


def fake_process(tmp_path, argv):
    proc = tmp_path / "proc"
    process = proc / "99999999"
    process.mkdir(parents=True)
    command = process / "cmdline"
    command.write_bytes(b"\0".join(os.fsencode(argument) for argument in argv) + b"\0")
    (process / "fd").mkdir()
    (process / "maps").write_text("")
    return proc, command


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/python3", "/synthetic/.venv/bin/pip", "install", "example"],
        ["/usr/bin/python3.12", "/synthetic/.venv/bin/pip3.12", "install", "example"],
        ["/synthetic/python tools/python3.12", "-I", "-B", "/synthetic/pip tools/pip3", "install", "example"],
    ],
    ids=["entry-script", "versioned-entry-script", "space-paths-and-flags"],
)
def test_python_package_entrypoints_defer_real_private_cache_removal(tmp_path, monkeypatch, request, argv):
    proc, command = fake_process(tmp_path, ["/usr/bin/python3", "-m", "pip", "install", "example"])
    cache = tmp_path / "cache"
    cache.mkdir()
    candidate = cache / "closed-download"
    candidate.write_bytes(b"synthetic" * 1024)
    allocated = candidate.stat().st_blocks * 512
    assert allocated > 1
    original_usage = retention.usage
    monkeypatch.setattr(retention, "usage", lambda: original_usage(proc))
    monkeypatch.setattr(retention, "DOWNLOAD_ROOTS", (cache,))
    monkeypatch.setattr(retention, "LOG_ROOTS", ())

    control = retention.retain(3, 1, True, False)
    assert candidate.exists()
    assert control["busy_or_unknown_usage"] is True
    assert control["download_candidate_files"] == 1 and control["removed_download_files"] == 0
    assert control["download_after_bytes"] == allocated

    command.write_bytes(b"\0".join(os.fsencode(argument) for argument in argv) + b"\0")
    result = retention.retain(3, 1, True, False)
    request.node.user_properties.extend(
        [("control", control), ("entrypoint", result), ("candidate_exists", candidate.exists())]
    )
    assert candidate.exists(), result
    assert result["busy_or_unknown_usage"] is True
    assert result["removed_download_files"] == result["removed_bytes"] == 0
    assert result["download_after_bytes"] == allocated


@pytest.mark.parametrize(
    "argv,busy",
    [
        (["/synthetic/bin/pip3.12", "install", "example"], True),
        (["/synthetic/bin/npm", "install"], True),
        (["/usr/bin/node", "/synthetic/npm/bin/npm-cli.js", "install"], True),
        (["/usr/bin/docker", "build", "."], True),
        (["/usr/bin/docker", "buildx", "build", "."], True),
        (["/synthetic/actions/bin/Runner.Worker", "spawnclient"], True),
        (["/usr/bin/python3", "-I", "-B", "-m", "pip", "install", "example"], True),
        (["/usr/bin/python3", "-IB", "-m", "pip", "install", "example"], True),
        (["/usr/bin/python3", "-W", "ignore", "-X", "dev", "-m", "pip"], True),
        (["/bin/sh", "-c", "/synthetic/bin/pip install example"], False),
        (["/usr/bin/python3", "-c", "pip install example"], False),
        (["/usr/bin/python3", "helper.py", "/synthetic/bin/pip", "install"], False),
        (["/usr/bin/python3", "-m", "other", "/synthetic/bin/pip"], False),
        (["/usr/bin/python3", "/synthetic/bin/pipeline.py"], False),
        (["/synthetic/bin/python-helper", "/synthetic/bin/pip"], False),
        (["/usr/bin/python3", "-X", "/synthetic/bin/pip"], False),
    ],
)
def test_busy_inventory_uses_the_actual_executable_or_python_operand(tmp_path, argv, busy):
    proc, _ = fake_process(tmp_path, argv)
    assert retention.usage(proc)[1] is busy
