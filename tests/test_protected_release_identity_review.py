"""Protected release identity follows configured roots and producer names.

Twenty-two distinct cases, no CLI/native repeats. All artifacts are synthetic.
Only unrelated collect() discovery and synthetic UID are replaced; checksums,
cold-registry empty handling, roots, binding and protected verification are real.
This checks verification receipts, not retention, deletion or installed health.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from scripts.disk_maintenance import inventory as inv


REVISION = "a" * 40
SOURCE_NAMES = (
    "autostop_manager/telegram_bridge.py",
    "deploy/systemd/autostop-work-telegram.service",
    "deploy/telegram/faster-whisper-small.sha256",
    "scripts/run-work-telegram-media.sh",
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _checksums(directory, names, target):
    rows = [f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  ./{name}\n" for name in names]
    _write(target, "".join(rows))


def _snapshot(root):
    result = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[name] = ("file", path.stat().st_mode, path.read_bytes())
        else:
            result[name] = ("directory", path.stat().st_mode)
    return result


@pytest.fixture
def layout(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ("manager_releases", "work_tg_releases", "tg_runtimes")}
    paths = {
        "manager": roots["manager_releases"] / f"20261016T073401Z-client-{REVISION[:12]}-100",
        "telegram": roots["work_tg_releases"] / f"20261016T073402Z-{REVISION[:12]}",
        "runtime": roots["tg_runtimes"] / REVISION,
    }
    for path in paths.values():
        path.mkdir(parents=True)
    for name in SOURCE_NAMES:
        for component in ("manager", "telegram"):
            _write(paths[component] / name, f"Synthetic trusted payload: {name}\n")
    manager, telegram, runtime = (paths[name] for name in ("manager", "telegram", "runtime"))
    _write(manager / "REVISION", REVISION + "\n")
    _checksums(manager, (*SOURCE_NAMES, "REVISION"), manager / "MANIFEST.sha256")
    for marker in (".dependencies-ready", ".model-ready"):
        _write(runtime / marker, "")
    _write(runtime / "model/model.bin", "Synthetic model bytes\n")
    _checksums(runtime / "model", ("model.bin",), runtime / "faster-whisper-small.sha256")
    (manager.parent / "current").symlink_to(manager, target_is_directory=True)
    (telegram.parent / "current").symlink_to(telegram, target_is_directory=True)
    policy = {
        "inventory": {
            "roots": {key: str(value) for key, value in roots.items()},
            "coherent_keep_paths": [[str(paths[name]) for name in ("manager", "telegram", "runtime")]],
            "pinned_paths": [],
            "cold_tg_runtimes": [],
        }
    }
    calls = []

    def synthetic_collect(actual_policy):
        assert actual_policy is policy
        assert set(actual_policy["inventory"]["roots"]) == set(roots)
        configured = list(actual_policy["inventory"]["roots"].values())
        configured += [value for row in actual_policy["inventory"]["coherent_keep_paths"] for value in row]
        assert all(Path(value).is_absolute() and Path(value).is_relative_to(tmp_path) for value in configured)
        calls.append(True)
        return {"keepers": {}, "protected": [], "fingerprint": "synthetic-protected-identity"}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verification attempted live discovery or an external boundary")

    monkeypatch.setattr(inv, "OWNER_UID", os.geteuid())
    monkeypatch.setattr(inv, "collect", synthetic_collect)
    for name in ("run", "checked_run", "process_references", "unit_references"):
        monkeypatch.setattr(inv, name, forbidden)
    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    return SimpleNamespace(base=tmp_path, roots=roots, paths=paths, policy=policy, calls=calls)


def _verify(layout):
    before = _snapshot(layout.base)
    policy_before = deepcopy(layout.policy)
    try:
        result = inv.verify_protected(layout.policy)
    finally:
        assert _snapshot(layout.base) == before
        assert layout.policy == policy_before
        assert layout.calls == [True]
    assert result["ok"] is True
    assert result["fingerprint"] == "synthetic-protected-identity"
    expected = {value for row in layout.policy["inventory"]["coherent_keep_paths"] for value in row}
    assert {receipt["path"] for receipt in result["receipts"]} == expected
    assert all(receipt["ok"] is True and receipt["component"] == "coherent_runtime" for receipt in result["receipts"])
    return result


def _relocate(layout, component, destination):
    original = layout.paths[component]
    destination.parent.mkdir(parents=True, exist_ok=True)
    original.rename(destination)
    if component in {"manager", "telegram"}:
        current = original.parent / "current"
        current.unlink()
        current.symlink_to(destination, target_is_directory=True)
    for row in layout.policy["inventory"]["coherent_keep_paths"]:
        row[:] = [str(destination) if value == str(original) else value for value in row]
    layout.paths[component] = destination


@pytest.mark.parametrize("variant", ("legacy_telegram", "telegram_with_revision", "coordinated_manager"))
def test_supported_release_layouts_are_verified(layout, variant):
    if variant == "telegram_with_revision":
        _write(layout.paths["telegram"] / "REVISION", REVISION + "\n")
    elif variant == "coordinated_manager":
        name = f"20261016T073401Z-{'b' * 12}-100-manager-{REVISION[:12]}"
        _relocate(layout, "manager", layout.roots["manager_releases"] / name)
    _verify(layout)
    assert not (layout.paths["telegram"] / "MANIFEST.sha256").exists()


@pytest.mark.parametrize(
    "defect", ("missing_revision", "short_revision", "wrong_revision", "missing_manifest", "source")
)
def test_manager_requires_its_revision_and_manifest(layout, defect):
    manager = layout.paths["manager"]
    # Isolate this Manager's own checks from redundant Telegram binding checks.
    layout.policy["inventory"]["coherent_keep_paths"] = [[str(manager)]]
    if defect == "missing_revision":
        (manager / "REVISION").unlink()
    elif defect == "short_revision":
        _write(manager / "REVISION", REVISION[:12] + "\n")
    elif defect == "wrong_revision":
        _write(manager / "REVISION", "b" * 40 + "\n")
    elif defect == "missing_manifest":
        (manager / "MANIFEST.sha256").unlink()
    else:
        _write(manager / SOURCE_NAMES[0], "Changed synthetic Manager source\n")
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)


@pytest.mark.parametrize("marker", (REVISION[:12], REVISION[:12] + "b" * 28), ids=("short", "same-prefix-wrong-sha"))
def test_optional_telegram_revision_must_equal_full_bound_manager_sha(layout, marker):
    _write(layout.paths["telegram"] / "REVISION", marker + "\n")
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)


def test_legacy_telegram_still_checks_source_against_manager(layout):
    _write(layout.paths["telegram"] / SOURCE_NAMES[0], "Changed synthetic Telegram source\n")
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)


@pytest.mark.parametrize("defect", ("short_name", "dependencies_marker", "model_marker", "model_bytes"))
def test_runtime_requires_full_sha_readiness_and_model_checksum(layout, defect):
    runtime = layout.paths["runtime"]
    if defect == "short_name":
        _relocate(layout, "runtime", runtime.with_name(REVISION[:12]))
    elif defect == "dependencies_marker":
        (runtime / ".dependencies-ready").unlink()
    elif defect == "model_marker":
        (runtime / ".model-ready").unlink()
    else:
        _write(runtime / "model/model.bin", "Changed synthetic model\n")
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)


def test_unknown_name_in_allowed_root_cannot_receive_verified_receipt(layout):
    unknown = layout.roots["manager_releases"] / "unrecognized-protected-snapshot"
    unknown.mkdir()
    layout.policy["inventory"]["coherent_keep_paths"].append([str(unknown)])
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)


@pytest.mark.parametrize("component", ("manager", "telegram", "runtime"))
def test_recognizable_release_outside_its_configured_root_is_rejected(layout, component):
    foreign = layout.base / "outside-configured-root" / layout.paths[component].name
    _relocate(layout, component, foreign)
    if component != "telegram":
        # Avoid another component's binding failure masking this root check.
        layout.policy["inventory"]["coherent_keep_paths"] = [[str(foreign)]]
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)


@pytest.mark.parametrize("entry", ("directory", "regular_symlink", "broken_symlink"))
def test_present_telegram_revision_must_be_a_regular_file(layout, entry):
    marker = layout.paths["telegram"] / "REVISION"
    if entry == "directory":
        marker.mkdir()
    else:
        target = layout.base / "synthetic-revision-target"
        assert target.is_relative_to(layout.base)
        if entry == "regular_symlink":
            _write(target, REVISION + "\n")
        else:
            assert not target.exists()
        marker.symlink_to(target)
        assert marker.is_symlink()
    with pytest.raises(inv.MaintenanceError):
        _verify(layout)
