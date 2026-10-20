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
